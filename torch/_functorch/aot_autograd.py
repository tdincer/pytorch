import collections
import dataclasses
import itertools
import logging
import warnings
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from enum import Enum
from functools import partial, wraps
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, NewType

from functorch import make_fx

import torch
import torch.fx.traceback as fx_traceback
import torch.nn as nn
import torch.utils._pytree as pytree
import torch.utils.dlpack
from torch import Tensor
from torch._dispatch.python import enable_python_dispatcher
from torch._dynamo.utils import dynamo_timed, format_graph_code
from torch._logging import getArtifactLogger
from torch._subclasses import CrossRefFakeMode, FakeTensor, FakeTensorMode
from torch.fx import immutable_collections, Interpreter
from torch.fx.experimental.proxy_tensor import is_sym_node, py_sym_types
from torch.fx.experimental.symbolic_shapes import ShapeEnv
from torch.multiprocessing.reductions import StorageWeakRef
from torch.nn.utils import stateless
from . import config
from .partitioners import default_partition
from torch._guards import TracingContext, DuplicateInputs, Source

log = logging.getLogger(__name__)
aot_joint_log = getArtifactLogger(__name__, "aot_joint_graph")
aot_graphs_log = getArtifactLogger(__name__, "aot_graphs")

MutationType = Enum(
    "MutationType", ("none", "metadata_only", "data", "data_and_metadata")
)
OutputType = Enum(
    "OutputType", (
        # output is not an alias
        "non_alias",
        # output aliases an input
        "alias_of_input",
        # output **is** an input tensor
        "is_input",
        # output has a ._base tensor, which is a graph intermediate.
        # We need to return its ._base as a graph output,
        # so its requires_grad info is populated correctly.
        # Instructs the runtime code to regenerate the current output
        # from a base tensor, graph_intermediates[base_idx]
        "alias_of_intermediate_save_as_output",
        # Same as above; but we don't need to explicitly add its ._base
        # as a graph output, because it already **is** a graph output.
        "alias_of_intermediate",
        # Same as above; but the output's ._base is **already** a user output.
        # Instructs the runtime code to regenerate the current output from
        # a base tensor, user_outputs[base_idx]
        "alias_of_intermediate_base_is_user_output",
        # See Note [Intermediate Bases Optimization]
        "unsafe_view_alias",
    )
)

pytree._register_pytree_node(
    immutable_collections.immutable_list,
    lambda x: (list(x), None),
    lambda x, c: immutable_collections.immutable_list(x),
)
pytree._register_pytree_node(
    immutable_collections.immutable_dict,
    lambda x: (list(x.values()), list(x.keys())),
    lambda x, c: immutable_collections.immutable_dict(
        dict(zip(c, x))
    ),
)

aten = torch.ops.aten

# This global counter increments every time we compile a graph with
# AOTAutograd.  You can use this to correlate runtime error messages
# with compile time (e.g., if you get an error at runtime saying
# compiled graph 3 failed, you can set a breakpoint at compile time
# for this graph number to investigate further at compile time.)
#
# NB: this is different from get_aot_compilation_context, which tracks
# each underlying graph that is compiled.  In contrast, AOT_COUNTER
# corresponds to top-level invocations of aot_module/aot_function;
# one counter is allocated per entire compiled block (but this block
# may involve compiling multiple subgraphs; e.g., for forwards/backwards)
AOT_COUNTER = itertools.count()

KNOWN_TYPES = tuple(
    [torch.Tensor, int, str, float, bool, type(None)] + list(py_sym_types)
)


@contextmanager
def preserve_rng_state():
    with torch.utils._python_dispatch._disable_current_modes():
        rng_state = torch.clone(torch.random.get_rng_state())
        if torch.cuda.is_available():
            cuda_rng_state = torch.clone(torch.cuda.get_rng_state())
    try:
        yield
    finally:
        with torch.utils._python_dispatch._disable_current_modes():
            torch.random.set_rng_state(rng_state)
            if torch.cuda.is_available():
                torch.cuda.set_rng_state(cuda_rng_state)


# Set up hooks so that during backward the fx's stack_trace is properly set
callback_set = False


def setup_stacktrace_preservation_hooks(roots: List):
    def iter_graph(roots):
        if not roots:
            return
        seen = set()
        q = collections.deque()
        for node in roots:
            if node is not None:
                seen.add(node)
                q.append(node)

        while q:
            node = q.popleft()
            for fn, _idx in node.next_functions:
                if fn in seen or fn is None:
                    continue
                seen.add(fn)
                q.append(fn)

            yield node

    def get_callback(saved_stack_):
        def callback():
            global callback_set
            fx_traceback.set_stack_trace(saved_stack_)
            callback_set = False

        return callback

    def get_prehook(stack_):
        def prehook(grad_output):
            global callback_set

            if not callback_set:
                torch.autograd.variable.Variable._execution_engine.queue_callback(
                    get_callback(fx_traceback.format_stack())
                )
                callback_set = True

            fx_traceback.set_stack_trace(stack_)

        return prehook

    def get_posthook(special_stack_):
        def posthook(grad_input, grad_output):
            fx_traceback.set_stack_trace(special_stack_)

        return posthook

    for node in iter_graph(roots):
        forward_node_stack = node.metadata.get("traceback_", [])
        node.register_prehook(get_prehook(forward_node_stack))

        special_stack = forward_node_stack.copy()
        special_stack.append(
            "Gradient addition node due to multiple use of tensor around:"
        )
        node.register_hook(get_posthook(special_stack))


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
# AOT Autograd contains a pretty non-trivial amount of logic to handle edge cases around aliasing and mutation
# that are external to the graph (they show up as side effects in some way when you run the graph).
#
# Take a look at `test_aotdispatch.py TestAOTAutograd.test_input_mutation*` tests for some examples functions
# and what they're compiled graphs looks like.
# Below is a very long comment detailing several edge cases, and showing how AOT Autograd handles them.
#
# Note [AOT Autograd: input data mutations]
#
# If we compile a function that mutates inputs, then those input mutations are real side effects
# that a user expects to see after running the compiled graph.
# However, the graph that we want to send to a backend needs to be *entirely* functional.
# The way we reconcile this difference is that we remove the mutations completely from the graph that we compile
# but we update the graph to return (updated_inputs, user_outputs).
# In the epilogue that runs after the compiled graph is executed, we copy the updated inputs back to the originals.
#
# Example: original user code:
# def f(x):
#     x.mul_(2)
#     out = x.mul(3)
#     return out
#
# After AOT Autograd compiles, we end up with a:
# (a) compiled graph
# (b) autograd.Function.forward() method, that executes the compiled graph
# (c) wrapper function, that calls the autograd.Function.forward() and performs the epilogue
#
# The output of (a, b, c) are all written below.
#
# def compiled_forward_graph(x):
#     x_updated = x.mul(2)
#     out = x_updated.mul(3)
#     return x_updated, out
#
# # x_updated gets a gradient in the compiled backward
# def compiled_backward_graph(grad_x_updated, grad_out):
#     grad_x = ...
#     return grad_x
#
# def autograd.Function.forward(x):
#     x_updated, out = compiled_forward_graph(x)
#     return x_updated, out
#
# def compiled_wrapper(x):
#     x_updated, out = autograd.Function.apply(x)
#     x.copy_(x_updated)
#     return out
#
# Another important thing to note is that updated inputs (due to data mutations) *do* participate
# in the compiled backward graph! Since the compiled forward graph gets N extra outputs
# (due to updated inputs showing up as graph outputs),
# The compiled backward gets an additional N inputs.
# That way, during the x.copy_(x_updated) bit in the epilogue, gradients will flow from the updated input
# back to the original input.


# Note [AOT Autograd: input metadata mutations]
#
# For the same reason as input mutations, we also don't put input metadata mutations in the graph.
# Instead, we return the updated version of the input (a view), and mutate the input's metadata outside of the graph
#
# Example: original user code:
# def f(x):
#     x.t_()
#     out = x.mul(3)
#     return out
#
# AOT Autograd output (compiled graph, autograd.Function.forward(), wrapper function):
# def compiled_forward_graph(x):
#     x_updated = x.t()
#     out = x_updated.mul(3)
#     return x_updated, out
#
# # x_updated does *not* get a gradient in the compiled backward
# def compiled_backward_graph(grad_out):
#     grad_x = ...
#     return grad_x
#
# def autograd.Function.forward(x):
#     x_updated, out = compiled_forward_graph(x)
#     return x_updated, out
#
# def compiled_wrapper(x):
#     x_updated, out = autograd.Function.apply(x)
#     x.as_strided_(x_updated)
#     return out


# Note [AOT Autograd: outputs aliasing inputs or intermediates!]
#
# AOT Autograd needs special handling for outputs that alias graph inputs or intermediates!
# Why?
# (1) autograd.Function.forward() has a limitation, where views that returned in the forward cannot later be mutated.
# (2) views don't need to be compiled in the graph anyway - it's cheap to generate them outside of the compiled graph,
#     in an epilogue.
# For outputs that alias inputs, we do the following:
# (a) *still* return the aliased output as a graph output
# (b) In the AOT Autograd wrapper/epilogue, we don't return that aliased output. Instead, we use it to regenerate the output.
#
# For outputs that alias *intermediates*, we do the following:
# (a) Return the output in the compiled forward, **and** return it's ._base (a graph intermediates) as an output in the forward
# (b) Use (output, graph_intermediate) to regenerate the alias, and return that to the user (instead of the compiled fw output).
# You might wonder why we return the aliased output directly in the graph (and making the graph compute it),
# only to not return it and instead generate a fresh alias off of the intermediate,
# instead of (say) just storing metadata about the size/stride of the output somewhere to generate the alias. There are two reasons:
# (1) Getting the actual alias tensor allows us to use view-replay to generate the alias, instead of an as_strided() call
# (2) Inductor (and other backends) are free to change the memory format of graph outputs, if it results in better performance.
#     This can result in problems if a user later tries to .view() that output expecting it to have one set of strides,
#     when it has a different set of strides.
#     By including the view op directly in the graph, inductor takes that into account when deciding what memory format
#     the graph intermediate should be.
#
# Another important thing to note is how our traced backward() graph handles aliases.
# (this applies to outputs aliasing inputs, outputs aliasing intermediates,
#  *and* updated inputs returned in the compiled forward due to metadata-only mutations).
# Any outputs that alias (either inputs or intermediates) do NOT participate in the compiled backward graph
# It would be wasteful to include them in the compiled backward(), because we regenerate them eagerly
# at the end of the forward.
#
# Example: original user code:
# def f(x):
#     out1 = x.t()
#     intermediate = x.mul(2)
#     out2 = intermediate.view(-1)
#     return out1, out2
#
# AOT Autograd output (compiled graph, autograd.Function.forward(), wrapper function):
# def compiled_forward_graph(x):
#     out1 = x.t()
#     intermediate = x.mul(2)
#     out2 = intermediate.view(-1)
#     # the compiled graph also returns the intermediate
#     return out1, out2, intermediate
#
# # intermediate gets a gradient in the compiled backward.
# # both output aliases (out1 and out2) do not.
# def compiled_backward_graph(grad_intermediate):
#     grad_x = ...
#     return grad_x
#
# def autograd.Function.forward(x):
#     out1, out2, intermediate = compiled_forward_graph(x)
#     return out1, out2, intermediate
#
# def compiled_wrapper(x):
#     out1, out2, intermediate = autograd.Function.apply(x)
#     # regenerate out1 from the input
#     out1_regenerated = out1._view_func(x)
#     # regenerate out1 from the intermediate
#     out2_regenerated = out2._view_func(intermediate)
#     return out1_regenerated, out2_regenerated


# Note [AOT Autograd: mutations to inputs that alias other inputs]
#
# Another edge case that is (only partially) handled today is when an input is mutated, but itself aliases another input.
# AOT Autograd needs to **ensure** that functionalization knows that the two inputs are aliased to each other.
# That way, when the aliased input is accessed later in the graph, functionalization knows to "update" the alias
# given the mutation that occurred.
#
# This is handled by updating the calling convention: we create a "synthetic base" that becomes a new input
# in the compiled function, and we regenerate the original (aliased) inputs directly off of the base
# inside of the compiled function.
#
# This logic is fully encapsulated in aot_wrapper_synthetic_base()
#
# Example: original user code:
# def f(x, x_view):
#     x.mul_(2)
#     out = x * x_view
#     return out
# f(x, x.view(-1))
#
# AOT Autograd output (compiled graph, autograd.Function.forward(), wrapper function):
# def compiled_forward_graph(base)
#     x = generate_x(base)
#     x_view = generate_x_view(base)
#     x_updated = x.mul(2)
#     x_view_updated = x_updated.view(-1)
#     out = x_updated * x_view_udpated
#     return x_updated, out
#
# # The calling convention change from (aliases) -> (base) happens
# # *outside* of the autograd.Function.forward().
# # That means the forward() only has 1 input (base),
# # and the backward() only has 1 output (grad_base)
# def compiled_backward_graph(grad_out):
#     grad_base = ...
#     return grad_base
#
# def autograd.Function.forward(base):
#     x_updated, out = compiled_forward_graph(base)
#     return x_updated, out
#
# # The compiled wrapper is where we create synthetic bases.
# # The info on which inputs are mutated is also tracked *before* synthetic base creation.
# def compiled_wrapper(x, x_view):
#     base = merge_view_inputs(x, x_view)
#     x_updated, out = autograd.Function.apply(base)
#     # x and x_view are aliased in eager mode, so this mutation to x will automatically affect x_view.
#     x.copy_(x_updated)
#     return out
#
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


# This class stores info about every user output.
@dataclass(frozen=True)
class OutputAliasInfo:
    # Tells us if this output is:
    # (1) a regular (non-aliased) output
    # (2) an alias of a forward input
    # (3) **is** a forward input (special case of "alias_of_input")
    # (4) an alias of an intermediate (aka an alias of an output of the inner traced forward)
    # (5) an alias of an intermediate, that explicitly requires returning the intermediate
    #     as a graph output
    # (6) an alias of an intermediate, where that intermediate is also a user output
    output_type: OutputType
    # The raw type of the output (torch.Tensor, SymInt, etc)
    raw_type: type
    # If (1) above, then
    # - base_idx is None
    # If (2) or (3) above, then
    # - Tells us that the base of this alias is user_fwd_input[base_idx]
    #   (This is an index into the inputs *before* we make synthetic bases)
    # If (4) or (5) above, then
    # - Tells us that the base of this alias is output_graph_intermediates[base_idx]
    #   here, this refers to the index of the *direct* traced
    # If (6) above, then:
    # - Tells us that the base of this alias is output_user_fwds[base_idx]
    #   here, this refers to the index of the *direct* traced
    base_idx: Optional[int]


# This class tells us info about user inputs.
@dataclass(frozen=True)
class InputAliasInfo:
    is_leaf: bool
    mutates_data: bool
    mutates_metadata: bool


# This class encapsulates all aliasing + mutation info we need about the forward graph
# See a more detailed overview of the edge case handling at
# https://docs.google.com/document/d/19UoIh_SVrMy_b2Sx5ZaeOJttm6P0Qmyss2rdBuyfoic/edit
@dataclass(eq=False)
class ViewAndMutationMeta:
    # length = # user inputs
    # This gives us info about every input, and what sort of mutation happened to it (if any)
    input_info: List[InputAliasInfo]

    # length = # user outputs
    # This gives us info about every output (mostly around whether it aliases other tensors)
    output_info: List[OutputAliasInfo]

    # length = # mutated inps + # user outputs
    # For every output *and* mutated input returned from the forward,
    # tells us whether or not the output should require gradients or not
    requires_grad_info: List[bool]

    # length = the number of intermediate bases appended as outputs to the end of the forward graph.
    # Note: this is not necessarily the same thing as:
    #   len([x for x in output_info if x.output_type == OutputType.alias_of_intermediate])
    # Because outputs might share a ._base, or an output's ._base might itself be
    # another user output (in both cases, we won't redundantly append bases to the end of the graph)
    num_intermediate_bases: int

    # For inference only: instructs us to keep data-only input mutations directly in the graph
    keep_input_mutations: int

    # length = (# inputs w data mutations) + (# user outputs that are non_aliasing tensors)
    #        + (# intermediate bases)
    # These are the FakeTensor (or potential SymInt) outputs that we traced from our
    # metadata pass of the user's forward function.
    # Their only use today is to pass them as a best-guess for tangents when tracing the joint.
    # Stashing them as part of our "metadata" makes it simpler if we want to run our analysis
    # pass once, and re-use the output throughout AOTAutograd
    traced_tangents: List[Any]

    def __post_init__(self):
        mutated_inp_indices = [
            i for i, m in enumerate(self.input_info) if m.mutates_metadata or m.mutates_data
        ]
        # pre-compute the indices of the inputs that are mutated.
        # When keep_input_mutations is set, we don't need to worry about our epilogue
        # handling data-only mutations, because we keep them directly in the graph.
        mutated_inp_runtime_indices = [
            i for i, m in enumerate(self.input_info) if m.mutates_metadata or (not self.keep_input_mutations and m.mutates_data)
        ]
        aliased_out_indices = [
            i
            for i, m in enumerate(self.output_info)
            if m.output_type not in [OutputType.non_alias, OutputType.unsafe_view_alias]
        ]

        self.mutated_inp_indices = mutated_inp_indices
        # This is pre-computed in post_init for perf.
        # It contains the index of every element
        # of input_info that corresponds to a mutation (data or metadata or both)
        self.mutated_inp_runtime_indices = mutated_inp_runtime_indices
        # This is pre-computed for perf.
        # It contains the index of every element
        # of output_info that corresponds to an alias (either of an input or intermediate)
        self.aliased_out_indices = aliased_out_indices
        self.num_outputs = len(self.output_info)
        self.num_outputs_non_aliased = len(
            [x for x in self.output_info if x.output_type in [OutputType.non_alias, OutputType.unsafe_view_alias]]
        )
        self.num_outputs_aliased_to_inputs = len(
            [
                x
                for x in self.output_info
                if x.output_type in [
                    OutputType.alias_of_input,
                    OutputType.is_input,
                ]
            ]
        )
        self.num_outputs_aliased_to_intermediates = len(
            [
                x
                for x in self.output_info
                if x.output_type in [
                    OutputType.alias_of_intermediate,
                    OutputType.alias_of_intermediate_save_as_output,
                    OutputType.alias_of_intermediate_base_is_user_output,
                ]
            ]
        )
        self.num_outputs_aliased = (
            self.num_outputs_aliased_to_inputs + self.num_outputs_aliased_to_intermediates
        )
        self.num_mutated_data_inputs = len(
            [x for x in self.input_info if x.mutates_data]
        )
        self.num_mutated_metadata_inputs = len(
            [
                x
                for x in self.input_info
                if x.mutates_metadata
            ]
        )
        self.num_mutated_metadata_only_inputs = len(
            [
                x
                for x in self.input_info
                if not x.mutates_data and x.mutates_metadata
            ]
        )
        self.num_mutated_inputs = self.num_mutated_data_inputs + self.num_mutated_metadata_only_inputs

    def __eq__(self, other):
        if not isinstance(other, ViewAndMutationMeta):
            return NotImplemented
        return (self.input_info == other.input_info and
                self.output_info == other.output_info and
                self.requires_grad_info == other.requires_grad_info and
                self.num_intermediate_bases == other.num_intermediate_bases and
                self.keep_input_mutations == other.keep_input_mutations and
                len(self.traced_tangents) == len(other.traced_tangents) and
                all(x.shape == y.shape and x.dtype == y.dtype for x, y, in zip(self.traced_tangents, other.traced_tangents)))


# This class exists because:
# - the autograd.Function.forward() in aot autograd returns outputs that might alias inputs
# - we only care about the metadata on those aliases, so we can regenerate them.
#   We do not want them to participate in the autograd.Function.
# We do that by wrapping them in an opaque class, so the autograd.Function
# does not know to treat them as tensors.
@dataclass(frozen=True)
class TensorAlias:
    alias: torch.Tensor


def has_same_metadata(t1, t2):
    return (
        t1.size() == t2.size()
        and t1.stride() == t2.stride()
        and t1.storage_offset() == t2.storage_offset()
    )


def gen_alias_from_base(aliased_base_tensor, target_meta_tensor, target_requires_grad):
    # Try to do view-replay if possible.
    # fall back to .as_strided() if we can't.
    if target_meta_tensor._base is not None:
        # The base that we want to replay our view off of might have a different shape than the view's original base.
        b = target_meta_tensor._base
        abt = aliased_base_tensor
        # Don't unnecessarily call as_strided if nothing changed; as_strided's
        # backward is poorly implemented and slow
        if abt is not b and (
            abt.size() != b.size() or
            abt.stride() != b.stride() or
            abt.storage_offset() != b.storage_offset()
        ):
            reshaped_base_tensor = aliased_base_tensor.as_strided(
                b.size(), b.stride(), b.storage_offset()
            )
        else:
            reshaped_base_tensor = aliased_base_tensor
        out = target_meta_tensor._view_func(reshaped_base_tensor)
        # This shape mismatch can happen due to a bug in inplace/view handling in autograd.
        # Try putting a breakpoint here and running
        # `test/functorch/test_aotdispatch TestAOTAutograd.test_output_all_alias_types`
        # Also, https://github.com/pytorch/pytorch/issues/49825
        #
        # As a stopgap, we'll fall back to as_strided.
        if out is not None and out.shape == target_meta_tensor.shape:
            if aliased_base_tensor.requires_grad and not target_requires_grad:
                out = out.detach()
            elif not aliased_base_tensor.requires_grad and target_requires_grad:
                out.requires_grad_(True)
            return out
    size = target_meta_tensor.size()
    stride = target_meta_tensor.stride()
    storage_offset = target_meta_tensor.storage_offset()
    if aliased_base_tensor.is_complex() and not target_meta_tensor.is_complex():
        aliased_out = torch.view_as_real(aliased_base_tensor).as_strided(
            size, stride, storage_offset
        )
    elif not aliased_base_tensor.is_complex() and target_meta_tensor.is_complex():
        aliased_out = torch.view_as_complex(aliased_base_tensor).as_strided(
            size, stride, storage_offset
        )
    else:
        aliased_out = aliased_base_tensor.as_strided(size, stride, storage_offset)
    # For outputs aliasing inputs, we need to check if the requires-gradness has changed.
    if aliased_base_tensor.requires_grad and not target_requires_grad:
        aliased_out = aliased_out.detach()
    elif not aliased_base_tensor.requires_grad and target_requires_grad:
        aliased_out.requires_grad_(True)
    return aliased_out

def to_fun(t):
    if isinstance(t, Tensor):
        return torch._to_functional_tensor(t, mirror_autograd_meta=True)
    else:
        return t

def from_fun(t):
    if not isinstance(t, Tensor) or not torch._is_functional_tensor(t):
        return t
    torch._sync(t)
    return torch._from_functional_tensor(t)


# This is a version of functionalization that is specifically designed
# for the AOTAutograd use case.
#
# Unlike functorch's variant, this doesn't use the functorch level system,
# instead it directly uses PyTorch's conventional dispatcher to hit the
# functionalization key.  In particular, this means that FunctionalTensorWrapper
# can have autograd data stored directly on it.
#
# In typical AOTAutograd usage, the dispatch key order will look like:
#
#   Autograd - Functionalization ~~~~> Proxy Mode - Fake Tensor
#       outer tensor                        inner tensor
#
# Returns:
# - ViewAndMutationMeta, telling us metadata about the inputs and outputs, and
#   The list of outputs from the forward, but **only** the outputs that we need
#   to pass in as tangents into the backward.
#   Specifically, aliased outputs from the forward get regenerated, and don't participate
#   in the compiled backward function.
def run_functionalized_fw_and_collect_metadata(
    f,
    *,
    keep_input_mutations: bool
) -> ViewAndMutationMeta:
    memo = {}

    def to_fun(t):
        if isinstance(t, Tensor):
            if t in memo:
                return memo[t]
            r = torch._to_functional_tensor(t, mirror_autograd_meta=True)
            memo[t] = r
            return r
        else:
            return t

    def from_fun(t):
        if not isinstance(t, Tensor) or not torch._is_functional_tensor(t):
            return t
        torch._sync(t)
        return torch._from_functional_tensor(t)

    @wraps(f)
    def inner(*flat_args):
        # This function is meant to be run with the forward, which expects a flat list of tensor/symint/other args.
        assert all(isinstance(a, KNOWN_TYPES) for a in flat_args)

        input_info: List[InputAliasInfo] = []
        output_info: List[OutputAliasInfo] = []
        input_requires_grad_info: List[bool] = []
        output_requires_grad_info: List[bool] = []

        flat_f_args = pytree.tree_map(to_fun, flat_args)

        torch._enable_functionalization(reapply_views=True)
        try:
            # precondition: The passed in function already handles unflattening inputs + flattening outputs
            flat_f_outs = f(*flat_f_args)
        finally:
            torch._disable_functionalization()

        # Inspect the state of the input tensor functional wrapper to detect input mutation info
        # If inp[i] has a metadata-only mutation, then maybe_inputs_with_mutated_metadata[i] contains the updated version
        for (i, (arg, f_arg)) in enumerate(zip(flat_args, flat_f_args)):
            if not isinstance(arg, Tensor):
                new_arg = arg
            else:
                torch._sync(f_arg)
                new_arg = torch._from_functional_tensor(f_arg)
            if arg is not new_arg:
                if StorageWeakRef(arg.untyped_storage()) == StorageWeakRef(new_arg.untyped_storage()):
                    mutates_data = False
                    mutates_metadata = True
                else:
                    mutates_data = True
                    mutates_metadata = not has_same_metadata(arg, new_arg)
                # Only track requires_grad info on *mutated* inputs,
                # because they show up in the autograd.Function.forward as outputs
                input_requires_grad_info.append(
                    isinstance(f_arg, torch.Tensor) and f_arg.requires_grad
                )
            else:
                mutates_data = False
                mutates_metadata = False

            input_info.append(InputAliasInfo(
                is_leaf=isinstance(arg, torch.Tensor) and arg.is_leaf,
                mutates_data=mutates_data,
                mutates_metadata=mutates_metadata
            ))

        # If a function involves creating a tensor, and returning a view of it, such that its _base is the intermediiate,
        # We need to make sure our graph returns the _base as a graph output, and we manually recreate the view
        # to return to the user. Why? The backend compiler is free to (incorrectly) not set requires_grad
        # on the base tensor, but we are obligated to properly set requires-gradness on the real output.

        num_mutated_inps = len(
            [x for x in input_info if x.mutates_data or x.mutates_metadata]
        )
        inp_storage_refs = {
            StorageWeakRef(inpt.untyped_storage()): idx
            for idx, inpt in enumerate(flat_f_args)
            if isinstance(inpt, torch.Tensor)
        }

        # We need inp tensor id's to be able to tell if an outputs **are** inputs.
        inp_tensor_ids = {
            id(inpt) for inpt in flat_f_args if isinstance(inpt, torch.Tensor)
        }
        # We need output tensor id's to tell if any output._base` attributes **are** other outputs.
        # (This is also a dict because we need to know that output's index, so we can regenerate
        # the alias from it).
        out_tensor_ids = {id(o): i for i, o in enumerate(flat_f_outs)}

        # Keep track of which outputs alias other outputs
        out_tensor_alias_counts = collections.defaultdict(int)
        for o in flat_f_outs:
            if isinstance(o, torch.Tensor):
                out_tensor_alias_counts[StorageWeakRef(o.untyped_storage())] += 1

        # maps the id of an intermediate base to its index in the output of the compiled forward
        intermediate_base_tensor_id_to_output_idx: Dict[int, int] = {}
        intermediate_bases: List[torch.Tensor] = []
        for o in flat_f_outs:
            if (
                isinstance(o, torch.Tensor)
                and StorageWeakRef(o.untyped_storage()) in inp_storage_refs
            ):
                base_idx = inp_storage_refs[StorageWeakRef(o.untyped_storage())]
                is_input_tensor = id(o) in inp_tensor_ids
                if is_input_tensor:
                    output_type = OutputType.is_input
                else:
                    output_type = OutputType.alias_of_input

            # We only need to handle the intermediate base case when both
            # the intermediate base and the output require gradients.
            # See Note [AOT Autograd: outputs aliasing inputs or intermediates!]
            elif (
                isinstance(o, torch.Tensor)
                and o._base is not None
                and o.requires_grad
                and o._base.requires_grad
            ):
                if out_tensor_alias_counts[StorageWeakRef(o.untyped_storage())] == 1:
                    # Note [Intermediate Bases Optimization]
                    # Normally if we have an output that aliases an intermediate,
                    # we need to add the extra "intermediate base" logic further down
                    # to prevent autograd from yelling at us if the user later tries to
                    # mutate that output.
                    # However, the common case here is if we have an output that aliases an intermediate,
                    # but doesn't alias any other outputs.
                    # In that case, autograd shouldn't have to worry about the aliasing at all
                    # (if that output is mutated, there are no other live aliases for autograd to worry about).
                    # The "intermediate bases" can hurt inductor perf by forcing more variables to become outputs.
                    # So as an optimization, we won't do intermediate base handling in this case.
                    # Instead, we'll hide the aliasing from autograd using aten._unsafe_view().
                    output_type = OutputType.unsafe_view_alias
                    base_idx = None
                else:
                    # First, check if o's ._base is an existing output
                    maybe_existing_out_idx = out_tensor_ids.get(id(o._base), None)
                    if maybe_existing_out_idx is not None:
                        # Special case where the output is an alias of a graph intermediate, but that intermediate
                        # is itself also a user output.
                        output_type = OutputType.alias_of_intermediate_base_is_user_output
                        base_idx = maybe_existing_out_idx
                    else:
                        # Next, check if o's ._base is an intermediate base that we already returned
                        maybe_existing_base_output_idx = intermediate_base_tensor_id_to_output_idx.get(
                            id(o._base), None
                        )
                        if maybe_existing_base_output_idx is not None:
                            output_type = OutputType.alias_of_intermediate
                            base_idx = maybe_existing_base_output_idx
                        else:
                            # Otherwise, take o._base and explicitly return it as an output in the compiled graph
                            new_out_idx = len(intermediate_bases)
                            base_idx = new_out_idx
                            # Indicate to the logic later on (when we trace the joint)
                            # that this particular output should get it's ._base appended to the forward graph outputs
                            output_type = OutputType.alias_of_intermediate_save_as_output
                            intermediate_base_tensor_id_to_output_idx[id(o._base)] = new_out_idx
                            intermediate_bases.append(o._base)
            else:
                output_type = OutputType.non_alias
                base_idx = None

            out_info = OutputAliasInfo(
                output_type=output_type,
                raw_type=type(o),
                base_idx=base_idx,
            )
            output_info.append(out_info)
            output_requires_grad_info.append(
                isinstance(o, torch.Tensor) and o.requires_grad
            )

        # Our autograd.Function.forward returns both mutated inputs and outputs,
        # so we need grad info on all of them.
        requires_grad_info = input_requires_grad_info + output_requires_grad_info
        assert len(requires_grad_info) == len(output_info) + len(
            [x for x in input_info if x.mutates_data or x.mutates_metadata]
        )

        # This analysis function returns *only* the outputs that are meant to be tangents to the backwards.
        # Anything that aliases (inputs returned in the fw due to metadata mutations, or outputs that alias inputs/intermediates)
        # are *regenerated* later, and not used directly in the autograd graph
        f_input_tangents = [
            inp
            for inp, info in zip(flat_f_args, input_info)
            if info.mutates_data
        ]
        f_output_tangents = [
            o
            for o, info in zip(flat_f_outs, output_info)
            if info.output_type in [OutputType.non_alias, OutputType.unsafe_view_alias] and issubclass(info.raw_type, torch.Tensor)
        ]
        # intermediate bases are also included in the backward graph
        f_tangents = f_input_tangents + f_output_tangents + intermediate_bases
        traced_tangents = pytree.tree_map(from_fun, f_tangents)

        metadata = ViewAndMutationMeta(
            input_info=input_info,
            requires_grad_info=requires_grad_info,
            output_info=output_info,
            num_intermediate_bases=len(intermediate_bases),
            keep_input_mutations=keep_input_mutations,
            traced_tangents=traced_tangents,
        )
        return metadata

    return inner

@dataclasses.dataclass
class AOTConfig:
    """
    Configuration for AOTDispatcher
    """

    fw_compiler: Callable
    bw_compiler: Callable
    partition_fn: Callable
    decompositions: Dict[Callable, Callable]
    num_params_buffers: int
    aot_id: int
    keep_inference_input_mutations: bool
    dynamic_shapes: bool = False
    aot_autograd_arg_pos_to_source : Optional[List[Source]] = None
    inference_compiler: Optional[Callable] = None

# This function takes in a tensor t, and returns one of t, t.view(), or t.clone().
# When tracing the joint forward + backward, for any inputs in the graph that are mutated,
# we need to clone them first (and similarly for metadata-only mutations, we need to view them first).
# The idea is that when we trace the backward, we need to pass in the *original* primals
# to autograd.grad(), before they were mutated.
# Note: when we have synthetic base inputs, we need to clone them *before* creating views off of them.
# This means that "idx" here represents the index of the (potentially) synthetic base.
# What we need to do is:
# (1) map the current (post-synthetic-base calling convention) input argument index
#     to int index pre-synthetic-base-calling-convention.
# (2) There could be multiple, if this index corresponds to a synthetic base
#     that has multiple input aliases.
# (3) If any of those corresponding inputs get metadata mutations, then we clone the base.
def maybe_to_fresh_input(idx, t, meta):
    if not isinstance(t, Tensor):
        return t
    if idx in meta.mutated_inp_indices:
        # We only need to bother cloning mutated inputs that participate in autograd.
        mutated_inp_idx = meta.mutated_inp_indices.index(idx)
        if meta.requires_grad_info[mutated_inp_idx] and meta.input_info[idx].mutates_data:
            # Make sure the primal we pass to autograd.grad()
            # sees the tensor before the mutation
            return t.clone()
        if meta.requires_grad_info[mutated_inp_idx] and meta.input_info[idx].mutates_metadata:
            # Make sure the primal we pass to autograd.grad()
            # sees the tensor before the metadata mutation
            return t.view(t.shape)
    return t

# This function returns a new function that returns mutated inputs as outputs.
# if keep_data_input_mutations is set, then we assume that data-only mutations
# will be left in the graph, and we only return metadata-mutated inputs as outputs.
def fn_input_mutations_to_outputs(
    fn: Callable,
    meta: ViewAndMutationMeta,
    keep_data_input_mutations: bool,
) -> Any:
    def inner_fn(*args):
        outs = fn(*args)
        assert len(meta.output_info) == len(outs)
        # The compiled fw will return mutated input tensors, *including* metadata-only mutation.
        # However, if keep_data_input_mutations is set, the compiled fw only needs to return metadata-mutated inputs.
        # (because data-only input mutations are handled directly in the compiled graph)
        mutated_inputs_to_return = [
            x
            for (i, x) in enumerate(args)
            if meta.input_info[i].mutates_metadata or (meta.input_info[i].mutates_data and not keep_data_input_mutations)
        ]
        return *mutated_inputs_to_return, *outs
    return inner_fn

# This function takes in a fn with external aliasing and mutation,
# and returns a new fn with no external aliasing and mutation,
# as needed for autograd.
# The main transformations are:
# - Return mutated inputs as extra outputs
# - Clone mutated inputs that require gradients,
#   because autograd will require us to pass the pre-mutated inputs into autograd.grad
# - Return intermediate bases of outputs as additional outputs,
#   needed to appease autograd.Function
# The new function returns:
# (1) The updated outputs
# (2) A boolean mask of len(new_fn_outputs),
#     that can be used to tell autograd.grad which outputs should get tangents
#     if we trace the backward.
def fn_prepped_for_autograd(
    fn: Callable,
    meta: ViewAndMutationMeta,
) -> Any:
    def inner_fn(*args):
        args_maybe_cloned = [
            maybe_to_fresh_input(i, t, meta) for i, t in enumerate(args)
        ]

        outs = fn(*args_maybe_cloned)
        assert isinstance(outs, (tuple, list))
        outs = list(outs)
        assert len(meta.output_info) == len(outs)

        mutated_inputs_to_return = [
            x
            for (i, x) in enumerate(args_maybe_cloned)
            if meta.input_info[i].mutates_metadata or meta.input_info[i].mutates_data
        ]

        intermediate_bases = []
        for i, (o, info) in enumerate(zip(outs, meta.output_info)):
            if info.output_type == OutputType.alias_of_intermediate_save_as_output:
                intermediate_bases.append(o._base)
            elif info.output_type == OutputType.unsafe_view_alias:
                # See Note [Intermediate Bases Optimization]
                outs[i] = torch.ops.aten._unsafe_view.default(o, o.shape)

        assert meta.num_intermediate_bases == len(intermediate_bases)

        # the compiled forward should return (mutated_inputs, user_outs, intermediate_bases)
        fw_outs_to_return = *mutated_inputs_to_return, *outs, *intermediate_bases

        # Also return a boolean mask specifying which outputs to this function will be used as tangents
        mutated_inputs_grad_mask = [
            meta.input_info[meta.mutated_inp_indices[i]].mutates_data
            for (i, x) in enumerate(mutated_inputs_to_return)
        ]

        # Pass any (non-aliased) outputs in as tangents, since they'll be returned as outputs in the fw
        # For outputs that are aliases of intermediates, we will have returned the output's _base as an output in the graph instead,
        # which we *should* send to grad()
        output_grad_mask = [
            meta.output_info[i].output_type in [OutputType.non_alias, OutputType.unsafe_view_alias]
            # Also, only tensor outputs should participate in the backward
            # (in particular, Symint outputs in the forward graph shouldn't get tangents)
            and issubclass(meta.output_info[i].raw_type, torch.Tensor)
            for (i, x) in enumerate(outs)
        ]

        intermediate_base_grad_mask = [True for _ in range(len(intermediate_bases))]

        out_grad_mask = mutated_inputs_grad_mask + output_grad_mask + intermediate_base_grad_mask
        assert len(out_grad_mask) == len(fw_outs_to_return)

        # Take care to grab and sync the updated inputs from primals_after_cloning (the inputs we actually mutate!)
        # and not primals (the preserved inputs, pre-mutation, that we pass to grad())
        # This is annoying: our joint function needs to be aware of functionalization
        # (syncing mutated inputs before calling autograd.grad())
        # In theory, we could make the autograd engine do this automatically, although that probably isn't any cleaner.
        for i, arg in enumerate(args_maybe_cloned):
            if not isinstance(arg, Tensor):
                continue
            torch._sync(arg)

        return fw_outs_to_return, out_grad_mask
    return inner_fn

# Given a fn, computes the joint.
# NOTE: fn is expects the following behavior:
# (1) fn() needs to return a tuple of (outs, mask),
#     where `mask` tells us which outputs are meant to have tangents.
#     we don't know this info automatically, because we don't actually want to blindly
#     compute tangents for every output that requires grad.
#     Specifically, outputs that alias inputs won't participate in the backward and get tangents.
# (2) fn() cannot mutate any inputs that require gradient.
#     otherwise, when we compute autograd.grad(), we will not take those input mutations into account
#     (the way this is handled is that we ensure any inputs that normally get mutated are cloned first)
def create_joint(
    fn: Callable,
) -> Any:
    def inner_fn(primals: List[Any], tangents: List[Any]):
        outs, tangent_mask = fn(*primals)
        assert len(tangent_mask) == len(outs)
        outs_to_grad = [o for needs_tangent, o in zip(tangent_mask, outs) if needs_tangent]
        assert len(outs_to_grad) == len(tangents)

        # Get the inputs that need gradients
        grad_primals = []
        inputs_needs_grads = []
        # Note that we're not using primals here,
        # being carefully not to pass any mutated inputs into autograd.grad()
        for p in primals:
            is_grad_tensor = isinstance(p, Tensor) and p.requires_grad
            inputs_needs_grads.append(is_grad_tensor)
            if is_grad_tensor:
                grad_primals.append(p)

        # Get the outputs that need gradients
        needed_outs = []
        needed_tangents = []
        for out, tangent in zip(outs_to_grad, tangents):
            if isinstance(out, Tensor) and out.requires_grad:
                # A bit sketchy, but fixes e.g. test_aot_autograd_exhaustive_matmul_cpu_float32
                # The issue is that we are sensitive to decomps that don't accurately maintain
                # their output's _base.shape compared to eager mode, and this helps mitigate a bit.
                needed_outs.append(
                    out if out.shape == tangent.shape else out.view(tangent.shape)
                )
                needed_tangents.append(tangent)

        setup_stacktrace_preservation_hooks([out.grad_fn for out in needed_outs])

        backward_out = []
        # Call the backwards pass
        if grad_primals:
            with fx_traceback.preserve_node_meta():
                backward_out = torch.autograd.grad(
                    needed_outs,
                    grad_primals,
                    grad_outputs=needed_tangents,
                    allow_unused=True,
                )
        backward_out_iter = iter(backward_out)
        return outs, [
            next(backward_out_iter) if i else None for i in inputs_needs_grads
        ]
    return inner_fn

# This creates the final function that we want to trace using make_fx(),
# in both aot_dispatch_autograd and aot_dispatch_base.
# Preconditions:
# - fn corresponds to the user's fw function
# - fn arguments have been flattened, duplicate arguments have been handled
# - In the returned function, the "primals" arguments *includes* synthetic bases.
# This function does the work of functionalizing the input function,
# and performing copy_() calls at the end of the function if `keep_input_mutations` is set.
# The function returned has signature that is either:
# (1) "traced_fn(primals: List[Any])" if trace_joint is False
# (2) "traced_fn(primals: List[Any], tangents: List[Any])" if trace_joint is True
def create_functionalized_graph(
    fn,
    args,
    *,
    meta: ViewAndMutationMeta,
    aot_config: AOTConfig,
    trace_joint: bool,
):
    def functionalized_f_helper(*args):
        # Wrap inputs into functional wrappers
        f_args = pytree.tree_map(to_fun, args)
        torch._enable_functionalization(reapply_views=True)
        try:
            # Run the joint
            f_outs = fn(*f_args)
        finally:
            torch._disable_functionalization()

        if aot_config.keep_inference_input_mutations and not trace_joint:
            # Note: This is a bit annoying. There's a layering issue here, where:
            # (1) functionalization needs to operate on **synthetic base** inputs, before unpacking them into the "real" inputs.
            # (2) For keep_input_mutations, we support tracing a call to copy_() directly on mutated inputs.
            #     However, we **only** want to support this for inputs that have data-only (and no metadata) mutations,
            #     because inductor (and backends in generally) would prefer not to see these (e.g. as_strided_(), resize_()).
            #     This makes it pretty difficult for this logic to operate on synthetic bases.
            # (3) In addition, there are cases where it's significantly cheaper to perform the copy on the individual
            #     (unpacked) input aliases, instead of the synthetic base.
            # Example case where (3) could be important:
            #
            #     def f(x, y):
            #         x.mul_(2)
            #         y.mul_(3)
            #         return x, y
            #    a = torch.ones(1'000'000)
            #    x, y = out(a[0:9], a[1:10])
            #
            # It would be much better to add copy_() calls into the graph for the two tiny slices, instead of materializing
            # a giant "updated synthetic base" and copying into a's entire storage.
            #
            # For now, we are pessimistically not performing the optimization from (3);
            # we will materialize an "updated" synthetic base, and copy it back to the synthetic input base.
            # This allows us to factor aot autograd much more nicely, since only one area of the code needs to worry
            # about synthetic bases.
            for i, (inpt_old, inpt_f) in enumerate(zip(args, f_args)):
                if not isinstance(inpt_f, torch.Tensor):
                    continue
                torch._sync(inpt_f)
                inpt_new = torch._from_functional_tensor(inpt_f)
                if meta.input_info[i].mutates_data and not meta.input_info[i].mutates_metadata:
                    # We found an input that had a (data-only) mutation.
                    # Since keep_input_mutations is set, we need to faithfully apply a copy_()
                    # so the compiler will see the input mutation in the graph.
                    assert inpt_new is not inpt_old
                    assert has_same_metadata(inpt_new, inpt_old)
                    inpt_old.copy_(inpt_new)

        return pytree.tree_map(from_fun, f_outs)

    # Kinda annoying, but needed to make sure that the fx graph we trace out has "primals"
    # and "tangents" as its input names (which are special-cased by the partitioner)
    def joint_helper(primals, tangents):
        return functionalized_f_helper(primals, tangents)

    def fwd_helper(*args):
        return functionalized_f_helper(*args)

    with enable_python_dispatcher():
        return make_fx(joint_helper if trace_joint else fwd_helper, decomposition_table=aot_config.decompositions)(*args)


def normalize_as_list(x):
    if isinstance(x, tuple):
        return list(x)
    elif isinstance(x, list):
        return x
    return [x]


aot_autograd_decompositions = {}


# This is a list since looking forward, we can have this arbitrarily nested.
graph_being_compiled: List[str] = []
# TODO: It would be nice to reset the numbering every time aot_id goes
# up, but this is annoying to do right now (because we don't know if
# an aot_id will come back from the dead), so right now this also happens
# to be a globally unique number too (at the cost of wobbling if you change
# how the graphs compile)
nth_graph: int = 0
model_name: str = "model"


def set_model_name(name):
    global model_name
    model_name = name


def get_aot_compilation_context() -> Tuple[List[str], str, int]:
    return list(graph_being_compiled), model_name, nth_graph


def get_aot_graph_name() -> str:
    """
    Returns the name of the graph being compiled.
    """
    global model_name, graph_being_compiled, nth_graph
    return f"{model_name}__{'_'.join(graph_being_compiled)}_{nth_graph}"


get_graph_being_compiled = get_aot_graph_name


@contextmanager
def track_graph_compiling(aot_config, graph_name):
    global graph_being_compiled
    # TODO: Don't shove the aot_id in here; set it in the context
    graph_being_compiled = [f"{aot_config.aot_id}_{graph_name}"]
    try:
        yield
    finally:
        global nth_graph
        nth_graph += 1
        graph_being_compiled = []


def make_boxed_func(f):
    def g(args):
        return f(*args)

    g._boxed_call = True
    return g


def make_boxed_compiler(compiler):
    @wraps(compiler)
    def f(fx_g, inps):
        out_f = compiler(fx_g, inps)
        fx_g = make_boxed_func(out_f)
        return fx_g

    return f


def call_func_with_args(f, args, steal_args=False, disable_amp=False):
    if not steal_args:
        args = list(args)
    assert isinstance(args, list)

    if disable_amp:
        guard = torch._C._DisableAutocast()
    try:
        if hasattr(f, "_boxed_call"):
            out = normalize_as_list(f(args))
        else:
            # TODO: Please remove soon
            # https://github.com/pytorch/pytorch/pull/83137#issuecomment-1211320670
            warnings.warn(
                "Your compiler for AOTAutograd is returning a function that doesn't take boxed arguments. "
                "Please wrap it with functorch.compile.make_boxed_func or handle the boxed arguments yourself. "
                "See https://github.com/pytorch/pytorch/pull/83137#issuecomment-1211320670 for rationale."
            )
            out = normalize_as_list(f(*args))
    finally:
        if disable_amp:
            del guard
    return out

def aot_dispatch_base(flat_fn, flat_args: List[Tensor], aot_config: AOTConfig, *, fw_metadata: ViewAndMutationMeta):
    # aot_dispatch_base requires functionalization, but doesn't need to handle as many cases as the autograd case.
    # The cases that aot_dispatch_base doesn't need to handle include:
    # - outputs that are aliases of graph intermediates
    # - outputs that are aliases of graph inputs
    # While cases that it does need to handle include:
    # - input mutations (including when inputs are aliases of each other)
    # - input metadata mutations
    keep_mutations = aot_config.keep_inference_input_mutations
    fn_to_trace = fn_input_mutations_to_outputs(
        flat_fn,
        fw_metadata,
        keep_data_input_mutations=aot_config.keep_inference_input_mutations,
    )
    fw_module = create_functionalized_graph(
        fn_to_trace,
        flat_args,
        meta=fw_metadata,
        aot_config=aot_config,
        trace_joint=False,
    )

    # As long as we opted to remove input mutations, then
    # there should be *NO* mutating ops in the graph at this point.
    copy_count = assert_functional_graph(fw_module.graph, allow_input_mutations=aot_config.keep_inference_input_mutations)

    fw_module.graph.eliminate_dead_code()
    fw_module.recompile()

    copy_count2 = assert_functional_graph(fw_module.graph, allow_input_mutations=aot_config.keep_inference_input_mutations)

    assert copy_count == copy_count2

    aot_graphs_log.info(format_graph_code(f"====== Forward graph {aot_config.aot_id} ======\n", fw_module))

    disable_amp = torch._C._is_any_autocast_enabled()
    context = disable_autocast_manager if disable_amp else nullcontext

    with context(), track_graph_compiling(aot_config, "inference"):
        compiler = aot_config.inference_compiler if aot_config.inference_compiler is not None else aot_config.fw_compiler
        compiled_fw = compiler(fw_module, flat_args)

    compiled_fn = create_runtime_wrapper(
        compiled_fw,
        runtime_metadata=fw_metadata,
        trace_joint=False,
        keep_input_mutations=aot_config.keep_inference_input_mutations,
        disable_amp=disable_amp
    )

    return compiled_fn


# Returns the number of detected copy_
def assert_functional_graph(fx_g: torch.fx.Graph, *, allow_input_mutations: bool = False) -> int:
    placeholders = set()
    copy_count = 0
    # NB: It would also be nice to verify that the mutations all happen at the
    # end, but we also do some administrative views after mutations so this
    # isn't actually true.  (TODO: Could this cause problems for Inductor?)
    for n in fx_g.nodes:
        if n.op == "placeholder":
            placeholders.add(n)
        if isinstance(n.target, torch._ops.OpOverload):
            if n.target is aten.copy_.default and allow_input_mutations:
                suffix = True
                # Can only copy_ into an input, and can only do so once
                assert n.args[0] in placeholders
                placeholders.remove(n.args[0])
                copy_count += 1
            else:
                assert not n.target._schema.is_mutable, \
                    f'aot_autograd expected to have an entirely functional graph, but found {n.format_node()}'
    return copy_count


@contextmanager
def disable_autocast_manager():
    guard = torch._C._DisableAutocast()
    try:
        yield
    finally:
        del guard


def are_differentiable_views(view1, view2):
    if view1 is view2:
        return True
    if view1._base is None and view2._base is None:
        return False
    if view1._base is view2._base or view1._base is view2 or view1 is view2._base:
        return True
    return False


def same_dtype_views(view1, view2):
    if view1.dtype != view2.dtype:
        return False
    if view1._base is not None and view1.dtype != view1._base.dtype:
        return False
    if view2._base is not None and view2.dtype != view2._base.dtype:
        return False
    return True


# Note [Handling mutations on an input that aliases other inputs]
# The easiest example to show-case this edge case is here:
#
# def f(a, b):
#     a.mul_(2)
#     out = a + b
#     return out
# b = torch.ones(...)
# a = b.view(-1)
# f(a, b)
#
# In this situation, if a and b happened to be aliased, we need to trace something different!
# Suppose we had b = a.view(-1)
# (In this case, that means that `a._base is b`)
#
# We need to ensure that the aliasing relationship between a and b is preserved.
# We do that detecting the specific situation above (mutate an input that aliases another input),
# and when we do that, we create a synthetic base argument. Then inside of the traced forward,
# we regenerate a and b off of that base.
# The complete example of the transformed function looks like this:
#
# // The traced forward takes in a synthetic base, and regenerates the aliased inputs as views
# // We could consider getting view-replay support here to minimize as_strided_scatter ops in the graph
# def traced_forward(base):
#     a = base.as_strided(...)
#     b = base.as_strided(...)
#     a_updated = a.mul(2)
#     base_updated = torch.as_strided_scatter(base, a_updated, ...)
#     b_updated = base_updated.as_strided(...)
#     out = a_updated + b_updated
#     return a_updated, out
#
# def compiled_fn(a, b):
#     // we detect that a is the "differentiable base" here
#     base = a
#     // In other situations, we might do either:
#     // (1) a and b are both views off of some larger differentiable base
#     //     assert a._base is b._base and a._base is not None
#     //     base = a._base
#     // (2) a and b both don't require gradients. Create a base from the storage
#     //     assert a._base is None and b._base is None
#     //     base = torch.Tensor(a.storage())
#     a_updated, out = traced_forward(base)
#     a.copy_(a_updated)
#     return out
#
# This function:
# (1) Merges input views into a synthetic base argument, when any of those input views are mutated
# (2) Returns metadata telling the autograd.Function how to modify their arguments properly,
#     to respect the new calling convention.
#
# The calling convention is as follows.
# Any inputs that were originally views of one another get yanked, and replaced with a synthetic base.
# The argument list ordering goes [base1, ..., baseN], [arg1, ..., argN],
# Where the ordering of the bases is determined from the ordering of the original view args.
# baseA will come before baseB if the earliest original argument coming from baseA
# showed up earlier in the argument list than the earliest original argument coming from baseB.
#
# Example, given some tensors a, b, c, d
# call site:
#   f(a, c.view(-1), b.view(-1), b, c, d)
# Modified argument list:
#   c_base comes first because the first c view came earlier in arg list than the first b view
#   a and d still show up in the modified arg list, but b and c don't- they're regenerated from their bases
#   b_base = torch.Tensor(b.storage())
#   c_base = torch.Tensor(c.storage())
#   f(c_base, b_base, a, d)
def merge_view_inputs(
    fwd_inputs: List[Any], mutated_input_info: List[InputAliasInfo],
    *,
    # The autograd case currently has more restrictions than the inference case.
    is_inference: bool,
) -> Tuple[List[Any], Optional[List[Union[int, Tuple[int, torch.Tensor]]]]]:
    assert len(fwd_inputs) == len(mutated_input_info)
    storage_ref_to_idx: Dict[StorageWeakRef, List[int]] = collections.defaultdict(list)
    base_args = []
    other_args = []
    for i, inpt in enumerate(fwd_inputs):
        if isinstance(inpt, Tensor):
            storage_ref = StorageWeakRef(inpt.untyped_storage())
            storage_ref_to_idx[storage_ref].append(i)
        else:
            other_args.append(inpt)
    # Note [Synthetic Base Info Metadata]
    # This list contains metadata that tells you what the i'th argument in the inner calling convention should be.
    # It's either:
    # - another int (corresponding to the index in the argument list of the element from the outer calling convention)
    # - idx, view_tensor, where we can generate the new output with view_tensor._view_func(old_args[idx])
    #   idx corresponds to which synthetic base from the outer calling context to view
    inner_calling_convention_meta: Dict[int, Union[int, Tuple[int, torch.Tensor]]] = {}
    for aliased_input_indices in storage_ref_to_idx.values():
        if len(aliased_input_indices) <= 1 or not any(
            # We only care about mutations that affect all aliases,
            # so metadata mutations on an input doesn't require us to do synthetic base handling.
            mutated_input_info[inpt_idx].mutates_data
            for inpt_idx in aliased_input_indices
        ):
            for curr_idx in aliased_input_indices:
                other_args.append(fwd_inputs[curr_idx])
            continue
        # We detected an input that was mutated, AND aliases with another input.
        # we need to replace this set of aliased inputs with a single synthetic base.
        # For now, I'm banning a bunch of cases. We expect dynamo to properly detect these cases
        # and error out. We can fix them later.
        # These checks are transitive, so we don't need to check every pair.
        for idx1, idx2 in zip(aliased_input_indices, aliased_input_indices[1:]):
            view1 = fwd_inputs[idx1]
            view2 = fwd_inputs[idx2]
            # The "inputs that are aliased but have different differentiable bases" case
            # is more complicated and hopefully pretty rare. Not currently handled.
            if not is_inference:
                assert are_differentiable_views(
                    view1, view2
                ), "aot_autograd() does not yet handle non-differentiable view input mutations."
            # Regenerating views when reinterpreting complex / real tensors seems non-trivial,
            # not handling for now
            assert same_dtype_views(
                view1, view2
            ), "aot_autograd() does not yet handle input mutations on views with different dtypes."
        non_none_bases = [
            fwd_inputs[i]._base
            for i in aliased_input_indices
            if fwd_inputs[i]._base is not None
        ]
        aliases_with_none_bases = [
            fwd_inputs[i] for i in aliased_input_indices if fwd_inputs[i]._base is None
        ]
        if len(non_none_bases) == 0:
            # Case where none of the aliases have a ._base
            # we generate a synthetic base without gradients, and generate views off of it
            # We hit this case when we have input tensors to the graph that share a storage,
            # but do not have a ._base field.
            # Wondering when we hit this case?
            # The _base field simply says that autograd knows about the aliasing relationship,
            # but sometimes we create tensors which are aliased out of the same storage but guaranteed
            # to be disjoint. In these cases, we will skip setting up the _base relationship
            # for performance reasons (because the fact that the tensors share the same storage
            # is unobservable unless you (1) do naughty things with resize_/as_strided
            # or (2) look at the storage--as we are doing here.)
            # One particular example of this is optimizer steps on the LSTM module:
            # LSTM parameters are packed into a contiguous storage for efficiency reasons when
            # calling cuDNN kernels, so when these parameters get passed to the optimizer we will
            # find they share the same storage, but do not have _base set since they are all disjoint.
            #
            # NOTE: There is one case where this is unsafe:
            # torch.Tensor(storage) will ALWAYS create a 1D tensor, which is not necessarily
            # the same shape as the "actual" base that the tensor came from.
            # For the most part this is fine, because we always use as_strided()
            # to generate the original aliased inputs again.
            # If we were to use view-replay though, this could cause the aliased views
            # to have incorrect sizes.
            example_idx = aliased_input_indices[0]
            example_alias = fwd_inputs[example_idx]
            # Note that this function is re-used at both trace time and rutnime.
            # At trace time, we're under a FakeMode so synthetic_base becomes a FakeTensor.
            synthetic_base = torch.empty((0,), dtype=example_alias.dtype, device=example_alias.device)
            # We don't actually have a convenient way of going from storage -> tensor,
            # So using set_() here (we suffer some minor overhead, but this case is rare).
            synthetic_base.set_(example_alias.untyped_storage())
        else:
            # Case where all of the aliases require gradients, and have the same _base.
            synthetic_base = non_none_bases[0]
            for other_base in non_none_bases[1:]:
                assert (
                    other_base is synthetic_base
                ), "aot_autograd() does not yet handle non-differentiable view input mutations."
            for alias in aliases_with_none_bases:
                assert (
                    alias is synthetic_base
                ), "aot_autograd() does not yet handle non-differentiable view input mutations."
        base_args.append(synthetic_base)
        for curr_view_idx in aliased_input_indices:
            curr_view = fwd_inputs[curr_view_idx]
            base_idx = len(base_args) - 1
            # We store just enough info here so that we can regenerate the view later.
            # Regeneration: curr_view._view_func(args[base_idx])
            inner_calling_convention_meta[curr_view_idx] = (base_idx, curr_view)
    if len(base_args) == 0:
        assert len(other_args) == len(fwd_inputs)
        # If no synthetic bases are necessary, just return the original inputs.
        return fwd_inputs, None
    else:
        # Otherwise, return:
        # (1) The new args according to the updated calling convention: (synthetic_bases, other_args)
        # (2) Metadata telling functionalization how to generate the inner argument list given the outer calling convention.
        #     We post-process it into a list, where meta[i] tells you info about the i'th argument in the inner calling convention.
        args_to_functionalization = base_args + other_args
        arg_to_old_idx_map = {arg: i for (i, arg) in enumerate(fwd_inputs)}
        for i, other_arg in enumerate(other_args):
            new_idx = len(base_args) + i
            old_idx = arg_to_old_idx_map[other_arg]
            inner_calling_convention_meta[old_idx] = new_idx
        # post process into a list
        post_processed_calling_convention_meta: List[Union[int, Callable]] = [
            -1 for _ in range(len(inner_calling_convention_meta))
        ]
        for k, v in inner_calling_convention_meta.items():
            post_processed_calling_convention_meta[k] = v
        # Quick assert: every argument in the inner calling convention should be accounted for.
        for x in post_processed_calling_convention_meta:
            assert x != -1
        return args_to_functionalization, post_processed_calling_convention_meta


def format_guard_bug_msg(aot_config, expected):
    return (
        f"At compilation time, graph {aot_config.aot_id} was compiled under the "
        f"assumption that {expected}, but at runtime this was not the case.  "
        "This indicates a guard bug in AOTAutograd or Dynamo, please file a bug to PyTorch."
    )


def remove_dupe_metadata(
    m: ViewAndMutationMeta,
    keep_arg_mask: List[bool],
) -> ViewAndMutationMeta:
    assert len(m.input_info) == len(keep_arg_mask)
    # Easy invariant: the first argument should never be a dupe (it will be kept)
    assert len(keep_arg_mask) > 0 and keep_arg_mask[0]
    dupe_to_dedup_idx = [0]
    for i, b in enumerate(keep_arg_mask[1:]):
        if b:
            dupe_to_dedup_idx.append(dupe_to_dedup_idx[-1] + 1)
        else:
            dupe_to_dedup_idx.append(dupe_to_dedup_idx[-1])

    # Filter dupe'd mutated inputs out of traced_tangents
    num_data_mutations = len([x for x in m.input_info if x.mutates_data])
    other_traced_tangents = m.traced_tangents[num_data_mutations:]
    inp_traced_tangents = m.traced_tangents[:num_data_mutations]
    filtered_inp_traced_tangents = [x for i, x in enumerate(inp_traced_tangents) if keep_arg_mask[m.mutated_inp_indices[i]]]
    traced_tangents = filtered_inp_traced_tangents + other_traced_tangents

    return ViewAndMutationMeta(
        input_info=[x for i, x in enumerate(m.input_info) if keep_arg_mask[i]],
        # requires_grad_info consists of (mutated_inputs, forward_outputs).
        # Need to remove only the duplicate entries that correspond to the mutated inputs.
        requires_grad_info=[
            x for i, x in enumerate(m.requires_grad_info)
            if i >= len(m.mutated_inp_indices) or keep_arg_mask[m.mutated_inp_indices[i]]],
        # For outputs that are views of inputs, we store the index of the input that the output
        # was generated from. Need to update that index to account for removed dupes.
        output_info=[
            OutputAliasInfo(
                output_type=o.output_type,
                raw_type=o.raw_type,
                base_idx=None if o.base_idx is None else dupe_to_dedup_idx[o.base_idx]
            )
            for o in m.output_info
        ],
        num_intermediate_bases=m.num_intermediate_bases,
        keep_input_mutations=m.keep_input_mutations,
        traced_tangents=traced_tangents,
    )

# Given our ViewAndMutation metadata, this fn constructs a new set of metadata,
# after adding synthetic base arguments to the function.
# Most of the work in this fn is slogging through all of the metadata corresponding to inputs,
# and updating it with our synthetic base calling convention.
#
# When config.debug_assert is set, we automatically regenerate the metadata
# and compare it to this output for sanity.
#
# In addition to the updated metadata, also return the list of input indices
# that will need to be updated in the synthetic base epilogue
def create_synthetic_base_metadata(
    m: ViewAndMutationMeta,
    # Maps each outer argument idx to its inner idx (or, if this outer arg is generated from a
    # synthetic base, you get a tuple of (i, TensorMeta), telling you the base tensor idx, and view metadata)
    synthetic_base_info: List[Union[int, Tuple[int, torch.Tensor]]],
    outer_args: List[Any],
    inner_args: List[Any],
) -> Tuple[ViewAndMutationMeta, List[int]]:

    S_Outer = NewType('S_Outer', int)
    S_Inner = NewType('S_Inner', int)
    synthetic_base_to_indices: Dict[S_Inner, List[S_Outer]] = {}
    for inner_idx in range(len(inner_args)):
        outer_aliased_indices_of_current_base_arg = [
            outer_idx for outer_idx, inner_idx_or_tuple in enumerate(synthetic_base_info)
            if (isinstance(inner_idx_or_tuple, int) and inner_idx_or_tuple == inner_idx)
            or (isinstance(inner_idx_or_tuple, tuple) and inner_idx_or_tuple[0] == inner_idx)
        ]
        synthetic_base_to_indices[inner_idx] = outer_aliased_indices_of_current_base_arg

    # given the requires_grad info on mutated inputs,
    # generate the requires_grad info on those same mutated inputs, but after constructing synthetic bases.
    input_infos = []
    mutated_inp_require_grad_info = []
    for _, outer_indices in synthetic_base_to_indices.items():
        # leaf-ness should be all-or-nothing for aliased tensor.
        # (aka if "a" and "b" are views, then a.is_leaf == b.is_leaf)
        any_leaf = any(m.input_info[x].is_leaf for x in outer_indices)
        all_leaf = all(m.input_info[x].is_leaf for x in outer_indices)
        assert any_leaf == all_leaf
        inpt_info = InputAliasInfo(
            # If len(outer_indices) > 1, then this input is a synthetic base.
            # The invariant is that to the rest of aot autograd, synthetic bases only show up if
            # one of their aliases gets a data mutation. And if any of their aliases get metadata
            # mutations, they will be hidden from the rest of aot autograd.
            mutates_data=True if len(outer_indices) > 1 else m.input_info[outer_indices[0]].mutates_data,
            mutates_metadata=False if len(outer_indices) > 1 else m.input_info[outer_indices[0]].mutates_metadata,
            is_leaf=any_leaf,
        )
        input_infos.append(inpt_info)
        # requires_grad_info consists of (mutated_inputs, forward_outputs).
        # For any mutated inputs that correspond to aliased inputs,
        # Need to replace them with their mutated synthetic base
        if inpt_info.mutates_data or inpt_info.mutates_metadata:
            mutated_inp_require_grad_info.append(any(m.requires_grad_info[x] for x in outer_indices))

    # Find any inputs that fulfill the following criteria:
    # (1) They are part of a synthetic base (because they alias another input,
    #      and at least one input experiences a data mutation)
    # (2) They experience a metadata mutation
    outer_aliased_arg_idx_with_metadata_mutations = [
        outer_idx for outer_idx, inpt_info in enumerate(m.input_info)
        if inpt_info.mutates_metadata and not isinstance(synthetic_base_info[outer_idx], int)
    ]

    # grab the original requires grad info on the outputs, except the ones from the mutated inputs
    num_original_input_data_mutations = len([x for x in m.input_info if x.mutates_data or x.mutates_metadata])
    output_grad_info = m.requires_grad_info[num_original_input_data_mutations:]
    input_metadata_mutation_grad_info = [
        outer_args[outer_idx].requires_grad for outer_idx in outer_aliased_arg_idx_with_metadata_mutations]
    input_metadata_output_info = [
        OutputAliasInfo(
            output_type=OutputType.alias_of_input,
            raw_type=torch.Tensor,
            base_idx=synthetic_base_info[outer_idx][0],
        ) for outer_idx in outer_aliased_arg_idx_with_metadata_mutations]
    existing_output_infos = [
        OutputAliasInfo(
            output_type=o.output_type,
            raw_type=o.raw_type,
            # Map the input idx pre-synthetic-bases to the new idx post-synthetic-bases
            base_idx=None if o.base_idx is None
            else synthetic_base_info[o.base_idx]
            if isinstance(synthetic_base_info[o.base_idx], int)
            else synthetic_base_info[o.base_idx][0])
        for o in m.output_info]

    num_outer_mutated_data_inps = len([x for x in m.input_info if x.mutates_data])
    inner_mutated_data_inps = [x for inner_idx, x in enumerate(inner_args) if input_infos[inner_idx].mutates_data]

    requires_grad_info = mutated_inp_require_grad_info + output_grad_info + input_metadata_mutation_grad_info
    output_info = existing_output_infos + input_metadata_output_info
    # Regenerate traced tangents to include mutated inputs including synthetic bases
    traced_tangents = inner_mutated_data_inps + m.traced_tangents[num_outer_mutated_data_inps:]

    return ViewAndMutationMeta(
        input_info=input_infos,
        requires_grad_info=requires_grad_info,
        output_info=output_info,
        num_intermediate_bases=m.num_intermediate_bases,
        keep_input_mutations=m.keep_input_mutations,
        traced_tangents=traced_tangents,
    ), outer_aliased_arg_idx_with_metadata_mutations

# MOTIVATION:
#
# When tracing functions for future execution, one must be careful not to pass
# in the same input tensor multiple times (e.g., f(x, x), as this can result
# in graphs that are ONLY valid if you later pass a new tensor in exactly the
# same way (e.g., f(y, y)).  (NB: we really mean duplicate; two distinct
# tensors that alias each other is a different situation that is covered by
# aot_dispatch_deduplicated_autograd). Here are two examples:
#
# (1) Suppose you have a function:
#
#   def f(x, y):
#       return x + y
#
# If you make_fx(f)(x, x), you will trace out:
#
#   def f(x, y):
#       return y + y
#
# Oops!
#
# (2) For most tensors x and y, you can compute f's gradient with respect to
# these to inputs by saying torch.autograd.grad(f(x, y), (x, y)).  However,
# if x is y, you will trace out a program that gets incorrect gradients:
#
#   >>> x = torch.randn(1, requires_grad=True)
#   >>> torch.autograd.grad(x + x, (x, x))
#   (tensor([2.]), tensor([2.]))
#
# In other words, the gradient is double-counted.  Deduplicating the arguments
# gives you an appropriate gradient:
#
#   >>> y = torch.randn(1, requires_grad=True)
#   >>> torch.autograd.grad(x + y, (x, y))
#   (tensor([1.]), tensor([1.]))
#
# HOW TO DEDUPLICATE:
#
# There are a few strategies, in order of preference:
#
# 1. For every duplicate argument to the function, detach it into
#    a separate leaf tensor, so that it is no longer duplicated.
#
#       PRO: The resulting compiled graph works for any configuration
#       of duplicated arguments.
#
#       CON: It does not (naively) work if you mutate the metadata of inputs:
#
#           def f(x, y):
#               x.transpose_(0, 1)
#               y.transpose_(0, 2)
#
#           x = torch.randn(2, 3, 4)
#           f(x, x)
#
#       The ordering of the transposes inside f dictates whether or not
#       you get [4, 2, 3] or [3, 4, 2].  This means that you cannot precompute
#       what metadata mutations should get applied to each input; you need to
#       assume they aren't duplicates (what we do today) or preserve
#       the original metadata mutations exactly in order, so that they work
#       for any duplicate configuration.
#
#       CON: It does not (naively) work if you mutate the data of inputs.
#       In particular, leaf tensors that require grad cannot be mutated,
#       this makes it impossible to differentiate with respect to the original
#       base.
#
# 2. For every duplicate argument to the function, remove it, so it is
#    no longer part of the "true" signature:
#
#       PRO: Implemented naively, it still works for metadata/data mutation.
#
#       CON: The resulting compiled graph is duplicate-specialized: it only
#       works if future calls duplicate arguments in exactly the same way.
#       Horribly, Dynamo doesn't guard on this at the moment.  But even if
#       it did, you could still end up recompiling a bunch of each duplicate.
#
# Our strategy is to do (1) if we can, and do (2) otherwise, erroring if
# Dynamo's guards are not enough.  In practice, this seems to cover
# everything.
#
def aot_wrapper_dedupe(
    flat_fn,
    flat_args: List[Tensor],
    aot_config: AOTConfig,
    *,
    compiler_fn,
    fw_metadata,
):
    # Use information about whether or not flat_fn mutates its arguments
    # or not to handle dupe args

    # Strategy 1: For any input that is not mutated, we can leafify it if we
    # need to remove a duplicate.
    leaf_flat_args = []
    args_set = set()
    ok = True

    for i, a in enumerate(flat_args):
        if not isinstance(a, torch.Tensor):
            leaf_flat_args.append(a)
        elif a not in args_set:
            args_set.add(a)
            leaf_flat_args.append(a)
        elif not fw_metadata.input_info[i].mutates_data and not fw_metadata.input_info[i].mutates_metadata:
            leaf_flat_args.append(a.detach().requires_grad_(a.requires_grad))
        else:
            ok = False
            break

    if ok:
        return compiler_fn(flat_fn, leaf_flat_args, aot_config, fw_metadata=fw_metadata)

    # Strategy 2: Duplicate specialize.
    #
    # In Haskell types, suppose you have:
    #
    #   add_dupe_args :: DedupedArgs -> Args
    #   remove_dupe_args :: Args -> DedupedArgs
    #
    #   compiler_fn
    #       :: (DedupedArgs -> R) -> DedupedArgs -> AOTConfig -> (DedupedArgs -> R)
    #   deped_compiler_fn
    #       :: (Args -> R) -> Args -> AOTConfig -> (Args -> R)
    #
    # Then the code below can be written in point-free style as:
    #
    #   deduped_compiler_fn f a c =
    #       compiler_fn (f . add_dupe_args) (remove_dupe_args a) c . remove_dupe_args
    #
    # Suppose you have:
    #
    #   [a, b, a, c]
    #
    # We want:
    #
    #   remove_dupe_args([a, b, a, c]) == [a, b, c]
    #   add_dupe_args([a, b, c]) == [a, b, a, c]
    #
    # This is done via (respectively):
    #
    #   seen_args = {a: 0, b: 1, c: 2}
    #   add_dupe_map = {  # how to get args from the deduped list
    #       0: 0,
    #       1: 1,
    #       2: 0,
    #       3: 2,
    #   }
    #   keep_arg_mask = [True, True, False, True]

    seen_args = {}
    keep_arg_mask = []
    add_dupe_map = {}
    duped_arg_len = len(flat_args)

    j = 0  # index into deduped_flat_args
    for i, t in enumerate(flat_args):
        if t in seen_args:
            keep_arg_mask.append(False)
            add_dupe_map[i] = seen_args[t]
            continue
        keep_arg_mask.append(True)
        seen_args[t] = j
        add_dupe_map[i] = j
        j += 1

    unique_args = j

    # NB: Hot path, avoid set lookups here
    # TODO: Can avoid the zip here too, probably
    def remove_dupe_args(args):
        return [t for t, keep in zip(args, keep_arg_mask) if keep]

    def add_dupe_args(args):
        return [args[add_dupe_map[i]] for i in range(duped_arg_len)]

    deduped_flat_args = remove_dupe_args(flat_args)

    # Update our input metadata to remove duped input metadata.
    updated_fw_metadata = remove_dupe_metadata(fw_metadata, keep_arg_mask)

    tracing_context = TracingContext.get()
    if tracing_context and aot_config.aot_autograd_arg_pos_to_source:
        # TODO(voz): This structure is 1:1, we could consider an alternate structure like
        # kept_pos:[dupe_arg_pos], however, add_dupe_map is 1:1 so we would need a new structure there,
        # which feels like needless complexity for a tiny bit of efficiency at this point.
        for dupe_arg_pos, kept_pos in add_dupe_map.items():
            if dupe_arg_pos != kept_pos:
                dupe_arg_source = aot_config.aot_autograd_arg_pos_to_source[dupe_arg_pos]
                kept_arg_source = aot_config.aot_autograd_arg_pos_to_source[kept_pos]
                tracing_context.guards_context.aotautograd_guards.append(DuplicateInputs(kept_arg_source, dupe_arg_source))

    @wraps(flat_fn)
    def wrapped_flat_fn(*args):
        return flat_fn(*add_dupe_args(args))

    if config.debug_assert:
        ref_fw_metadata = run_functionalized_fw_and_collect_metadata(
            wrapped_flat_fn,
            keep_input_mutations=fw_metadata.keep_input_mutations,
        )(*deduped_flat_args)
        assert ref_fw_metadata == updated_fw_metadata

    compiled_fn = compiler_fn(wrapped_flat_fn, deduped_flat_args, aot_config, fw_metadata=updated_fw_metadata)

    if not hasattr(compiled_fn, "_boxed_call"):
        compiled_fn = make_boxed_func(compiled_fn)

    @wraps(compiled_fn)
    def wrapped_compiled_fn(args):
        deduped_args = remove_dupe_args(args)
        args.clear()
        return compiled_fn(deduped_args)

    wrapped_compiled_fn._boxed_call = True

    # This can be uncommented when we properly guard for duplicates,
    # but right now we must not do it.
    # if not config.debug_assert:
    #     return wrapped_compiled_fn

    @wraps(wrapped_compiled_fn)
    def debugged_compiled_fn(args):
        # Test that the computed remove/add arg functions are an inverse
        new_args = add_dupe_args(remove_dupe_args(args))
        seen = {}
        for i, (x, y) in enumerate(zip(new_args, args)):
            seen[y] = None
            assert x is y, format_guard_bug_msg(
                aot_config,
                f"{describe_input(i, aot_config)} would be a duplicate of "
                f"{describe_input(add_dupe_map[i], aot_config)}",
            )
        # This is only an error if there is metadata mutation on both of
        # the duped arguments; in this case, we need to know what order
        # the metadata mutation applies in.  You'll get the correct result
        # otherwise, because a graph that assumes distinct inputs works if
        # you dupe the inputs (the gradient contributions from each input
        # will get summed up appropriately.)
        #
        # TODO: work out how to setup this assert correctly
        """
        assert len(seen) == unique_args, format_guard_bug_msg(aot_config,
            f"there would be {unique_args} distinct arguments"
        )
        """
        return wrapped_compiled_fn(args)

    debugged_compiled_fn._boxed_call = True

    return debugged_compiled_fn

# This layer handles the situation where you have two inputs that alias each other,
# and one of the inputs is mutated.
# We need to take special care to ensure that the mutation is applied to the other aliases in the graph.
#
# pre-condition: aot_wrapper_dedup has already run.
# (This function will in theory work if there are duplicate args.
# However, the synthetic base code path is a bit sub-optimal, and running with dupe'd inputs
# would cause us to hit that path more frequently).
def aot_wrapper_synthetic_base(
    flat_fn,
    flat_args: List[Tensor],
    aot_config: AOTConfig,
    *,
    fw_metadata: ViewAndMutationMeta,
    # Currently, the only reason we need to plumb this bool is because
    # the synthetic base code prohibits more cases in the autograd case than the inference case.
    needs_autograd: bool,
    compiler_fn,
):
    is_inference = not needs_autograd
    flat_args_with_synthetic_bases, synthetic_base_info = merge_view_inputs(
        flat_args, fw_metadata.input_info, is_inference=is_inference,
    )
    # Happy path: we don't need synthetic bases
    if synthetic_base_info is None:
        return compiler_fn(flat_fn, flat_args, aot_config, fw_metadata=fw_metadata)

    assert len(fw_metadata.input_info) == len(synthetic_base_info)

    # Update our forward metadata to take synthetic bases into account
    fw_metadata_updated, aliased_arg_idx_with_metadata_mutations = \
        create_synthetic_base_metadata(fw_metadata, synthetic_base_info, flat_args, flat_args_with_synthetic_bases)

    num_aliased_args_with_metadata_mutations = len(aliased_arg_idx_with_metadata_mutations)

    def unpack_synthetic_bases(primals: List[Any]) -> List[Any]:
        f_args_inner = []
        for inner_idx_or_tuple in synthetic_base_info:
            if isinstance(inner_idx_or_tuple, int):
                f_args_inner.append(primals[inner_idx_or_tuple])
            else:
                inner_base_idx, view_tensor = inner_idx_or_tuple
                base = primals[inner_base_idx]
                view_arg = gen_alias_from_base(
                    base, view_tensor, view_tensor.requires_grad
                )
                f_args_inner.append(view_arg)
        return f_args_inner

    @wraps(flat_fn)
    def wrapped_flat_fn(*args):
        unpacked_args = unpack_synthetic_bases(args)
        # This is a bit subtle. The goal of this entire function (aot_dispatch_synthetic_bases)
        # is to relieve the downstream logic from having to reason about mutations on inputs that alias
        # each other, by replacing aliased inputs with a synthetic base.
        # One area where this breaks down a bit however is if one of those aliased inputs
        # experienced a metadata mutation.
        # We are now obligated to reapply the metadata mutation directly to the user's input;
        # it isn't enough to apply mutations back to the synthetic base in the downstream logic.
        #
        # The way we handle this is by pretending that those aliased inputs that experience metadata mutations
        # are additional outputs in the user's forward function.
        # The downstream logic will just treat these as "user outputs that alias inputs".
        # However, we will manually grab them at runtime here, use them to reapply the metadata mutation
        # to the user inputs, and not return them to the user.
        aliased_args_with_metadata_mutations = [
            x for i, x in enumerate(unpacked_args) if i in aliased_arg_idx_with_metadata_mutations]
        if len(aliased_args_with_metadata_mutations) > 0:
            return *(flat_fn(*unpacked_args)), *aliased_args_with_metadata_mutations
        else:
            return flat_fn(*unpacked_args)

    if config.debug_assert:
        ref_fw_metadata = run_functionalized_fw_and_collect_metadata(
            wrapped_flat_fn,
            keep_input_mutations=fw_metadata.keep_input_mutations,
        )(*flat_args_with_synthetic_bases)
        assert ref_fw_metadata == fw_metadata_updated

    compiled_fn = compiler_fn(wrapped_flat_fn, flat_args_with_synthetic_bases, aot_config, fw_metadata=fw_metadata_updated)

    if not hasattr(compiled_fn, "_boxed_call"):
        compiled_fn = make_boxed_func(compiled_fn)

    @wraps(compiled_fn)
    def wrapped_compiled_fn(args):
        args_with_synthetic_bases, synthetic_base_info = merge_view_inputs(
            args, fw_metadata.input_info, is_inference=is_inference
        )
        assert synthetic_base_info is not None
        aliased_args_w_metadata_mutations = [args[i] for i in aliased_arg_idx_with_metadata_mutations]
        args.clear()
        outs = compiled_fn(args_with_synthetic_bases)
        if num_aliased_args_with_metadata_mutations > 0:
            # This code does not handle **all** input metadata mutations.
            # Instead, it only handles metadata mutations on inputs that were converted into synthetic bases
            # (which only happens if at least one aliased input experienced a data mutation).
            # e.g:
            # def f(a, b):
            #     a.mul_(2)
            #     b.t_(1, 0)
            # f(x.view(2, 2), x.view(2, 2))
            mutated_metadata_inps = outs[-num_aliased_args_with_metadata_mutations:]
            user_outs = outs[:-num_aliased_args_with_metadata_mutations]
            for inp, mutated_inp in zip(aliased_args_w_metadata_mutations, mutated_metadata_inps):
                inp.as_strided_(mutated_inp.size(), mutated_inp.stride(), mutated_inp.storage_offset())
            return user_outs
        return outs

    return wrapped_compiled_fn


def describe_input(i, aot_config):
    if i < aot_config.num_params_buffers:
        return f"parameter/buffer {i}"
    else:
        return f"input {i - aot_config.num_params_buffers}"

# The wrapper created by this function handles all of the runtime aliasing and mutation "epilogue" logic
# that needs to run after the compiled function.
#
# This function accepts a trace_joint flag, indicating whether or not we're generating the runtime
# epilogue for a forward-only inference graph, or for an autograd.Function.apply function.
# This is because there are some minor differences in how we treat these cases at runtime:
# - resize_() is currently handled in the inference case, but not fully handled in the autograd case.
# - the autograd cases inserts TensorAlias wrapper objects for outputs that alias inputs
def create_runtime_wrapper(
    compiled_fn,
    *,
    runtime_metadata: ViewAndMutationMeta,
    trace_joint: bool,
    keep_input_mutations: bool,
    disable_amp: bool
):
    if not hasattr(compiled_fn, "_boxed_call"):
        compiled_fn = make_boxed_func(compiled_fn)

    def runtime_wrapper(*args):
        if trace_joint:
            with torch.autograd._force_original_view_tracking(True):
                all_outs = call_func_with_args(
                    compiled_fn,
                    args,
                    disable_amp=disable_amp,
                )
        else:
            all_outs = call_func_with_args(
                compiled_fn,
                args,
                disable_amp=disable_amp,
            )

        num_mutated_inps = runtime_metadata.num_mutated_inputs
        num_metadata_mutated_inps = runtime_metadata.num_mutated_metadata_inputs
        num_intermediate_bases = runtime_metadata.num_intermediate_bases

        if keep_input_mutations:
            assert (
                len(all_outs)
                == num_metadata_mutated_inps + runtime_metadata.num_outputs + num_intermediate_bases
            )
            assert (
                len(runtime_metadata.mutated_inp_runtime_indices) == num_metadata_mutated_inps
            )
        else:
            assert (
                len(all_outs)
                == num_mutated_inps + runtime_metadata.num_outputs + num_intermediate_bases
            )
            assert (
                len(runtime_metadata.mutated_inp_runtime_indices) == num_mutated_inps
            )
        # Step 3: After running the compiled fw, apply updates to mutated inputs
        num_mutations_to_apply = len(runtime_metadata.mutated_inp_runtime_indices)
        if num_mutations_to_apply > 0:
            updated_inputs = all_outs[: num_mutations_to_apply]
            fw_outs = all_outs[num_mutations_to_apply :]

            for i, inpt_idx in enumerate(
                runtime_metadata.mutated_inp_runtime_indices
            ):
                meta = runtime_metadata.input_info[inpt_idx]
                if not meta.mutates_data and not meta.mutates_metadata:
                    continue
                original_inpt = args[inpt_idx]
                updated_inpt = updated_inputs[i]
                # TODO: add better resize_() support for autograd case.
                # Check for the case when an input has been resized.
                # Note: One important thing to check for is user code that calls inpt.storage().resize_().
                # We can't trace operations on storage into the graph, so we should get dynamo to graph break.
                # TODO: handle resize_() on inputs to a larger size.
                # This is actually non-trivial to detect, so we should probably just handle it
                # (or make dynamo detect).
                # We can't just check of original_inpt.storage_size != updated_inpt.storage_size,
                # Because the original_inpt might be a view of some larger tensor,
                # and updated_inpt is always densely packed.
                if not trace_joint and original_inpt.storage().size() != updated_inpt.storage().size():
                    original_inpt.resize_(updated_inpt.size())
                if meta.mutates_metadata and not meta.mutates_data:
                    if trace_joint:
                        assert isinstance(updated_inpt, TensorAlias)
                        updated_inpt = updated_inpt.alias
                    # We need to grab the size/stride/storage_offset from the compiled forward,
                    # and use that to mutate the metadata of the input
                    original_inpt.as_strided_(
                        updated_inpt.size(),
                        updated_inpt.stride(),
                        updated_inpt.storage_offset(),
                    )
                else:
                    if meta.mutates_data and meta.mutates_metadata:
                        original_inpt.as_strided_(
                            updated_inpt.size(),
                            updated_inpt.stride(),
                            updated_inpt.storage_offset(),
                        )
                    else:
                        assert meta.mutates_data
                    if meta.is_leaf and original_inpt.requires_grad:
                        # We can hit this situation in this case:
                        #   def f(x):
                        #       x.detach().mul_(2)
                        #       return x + 1
                        # AOTAutograd will see a mutation in the above case, and try to
                        # apply a copy_() here, in the epilogue.
                        # But if x required gradients, and is a leaf, then autograd
                        # will yell at us for trying to mutate it.
                        # However, it's only possible to end up in this scenario (like the above)
                        # if all of the mutations to the leaf input were non-autograd-tracking mutations
                        # (aka mutations under no_grad(), or on detached views).
                        # In that case, we fully want to hide the mutation from autograd, so detaching is ok.
                        original_inpt.detach().copy_(updated_inpt)
                    else:
                        original_inpt.copy_(updated_inpt)
        else:
            fw_outs = all_outs

        # Step 4: Manually regenerate any outputs that are aliased to inputs, instead of
        # compiling them.
        if runtime_metadata.num_outputs_aliased > 0:
            # The compiled forward also returned intermediate bases. We don't want to return them to the user.
            if runtime_metadata.num_intermediate_bases > 0:
                fw_outs_no_intermediate_bases = fw_outs[
                    : -runtime_metadata.num_intermediate_bases
                ]
                intermediate_bases = fw_outs[-runtime_metadata.num_intermediate_bases:]
            else:
                fw_outs_no_intermediate_bases = fw_outs
                intermediate_bases = []
            assert len(fw_outs_no_intermediate_bases) == len(runtime_metadata.output_info)

            fw_outs_including_aliases = []
            for i, (o, info) in enumerate(zip(
                fw_outs_no_intermediate_bases, runtime_metadata.output_info
            )):
                if info.output_type == OutputType.non_alias or info.output_type == OutputType.unsafe_view_alias:
                    fw_outs_including_aliases.append(o)
                    continue
                if trace_joint:
                    assert isinstance(o, TensorAlias)
                    o_ = o.alias
                else:
                    o_ = o
                o_grad = runtime_metadata.requires_grad_info[runtime_metadata.num_mutated_inputs + i]
                if info.output_type == OutputType.alias_of_input:
                    aliased_base_tensor = args[info.base_idx]
                    regenerated_out = gen_alias_from_base(aliased_base_tensor, o_, o_grad)
                    fw_outs_including_aliases.append(regenerated_out)
                    continue
                elif info.output_type == OutputType.is_input:
                    aliased_base_tensor = args[info.base_idx]
                    regenerated_out = aliased_base_tensor
                    fw_outs_including_aliases.append(regenerated_out)
                    continue
                elif info.output_type == OutputType.alias_of_intermediate:
                    base_tensor_list = intermediate_bases
                elif info.output_type == OutputType.alias_of_intermediate_save_as_output:
                    base_tensor_list = intermediate_bases
                else:
                    assert info.output_type == OutputType.alias_of_intermediate_base_is_user_output
                    base_tensor_list = fw_outs_no_intermediate_bases
                aliased_base_tensor = base_tensor_list[info.base_idx]
                # TODO: handle the custom autograd function case here.
                # We need a way to check whether a tensor came from a custom autograd fn from python,
                # AND a way to replay that custom view fn.
                regenerated_out = gen_alias_from_base(aliased_base_tensor, o_, o_grad)
                fw_outs_including_aliases.append(regenerated_out)
            return fw_outs_including_aliases
        else:
            return fw_outs
    return runtime_wrapper

# Has the precondition that there
# are no duplicate arguments in flat_args (e.g., the same Tensor
# object never shows up twice.  However, two tensor inputs MAY alias
# the same storage, so long as they have separate TensorImpls.)
def aot_dispatch_autograd(flat_fn, flat_args: List[Any], aot_config: AOTConfig, *, fw_metadata: ViewAndMutationMeta):
    # traced_tangents corresponds to the set of outputs in the traced forward that should get grad_outputs in the traced backward.
    # It includes outputs of the original forward, *and* any updated inputs due to input mutations.
    # However, it does *not* include any outputs that are aliases of inputs or intermediates, or any metadata-only input mutations.
    traced_tangents = pytree.tree_map(
        lambda x: x.detach().contiguous() if isinstance(x, Tensor) else x,
        fw_metadata.traced_tangents,
    )

    assert len(fw_metadata.requires_grad_info) == fw_metadata.num_mutated_inputs + fw_metadata.num_outputs
    joint_inputs = (flat_args, traced_tangents)
    disable_amp = torch._C._is_any_autocast_enabled()

    fn_prepared_for_autograd = fn_prepped_for_autograd(
        flat_fn,
        fw_metadata,
    )
    joint_fn_to_trace = create_joint(fn_prepared_for_autograd)

    if config.use_functionalize:
        fx_g = create_functionalized_graph(
            joint_fn_to_trace,
            joint_inputs,
            meta=fw_metadata,
            aot_config=aot_config,
            trace_joint=True,
        )

        # There should be *NO* mutating ops in the graph at this point.
        assert_functional_graph(fx_g.graph)
        # Redudant with the check above, but worth having in case tracing introduced
        # a fake tensor. Unlikely.
        # See Note: [Fake Modules and AOTAutograd]
        torch._dynamo.utils.assert_no_fake_params_or_buffers(fx_g)
        fx_g.graph.eliminate_dead_code()
        fx_g.recompile()
    else:
        # joint_forward_backward() now always runs with functionalization, and factoring it out
        # to make that toggleable is a bit painful.
        # aot autograd without functionalization is wrong anyway, so we error.
        raise AssertionError(
            "Graph partitioning without functionalization is not sound, we may introduce errors"
        )

    aot_joint_log.info(format_graph_code(f"====== Joint graph {aot_config.aot_id} =====\n", fx_g))

    with torch.no_grad():
        with track_graph_compiling(aot_config, "joint"):
            num_inner_fwd_outputs = fw_metadata.num_mutated_inputs + fw_metadata.num_outputs + fw_metadata.num_intermediate_bases
            fw_module, bw_module = aot_config.partition_fn(
                fx_g, joint_inputs, num_fwd_outputs=num_inner_fwd_outputs
            )
            fw_outs = [n for n in fw_module.graph.nodes if n.op == "output"][0].args[0]
            # we only need to bookkeep the symints that are saved for bw, not any symints
            # the user forward might have returned in its own output
            fw_outs_saved_for_bw = fw_outs[num_inner_fwd_outputs:]
            symint_outs_saved_for_bw = [
                n for n in fw_outs_saved_for_bw if is_sym_node(n)
            ]
            _num_symints_saved_for_bw = len(symint_outs_saved_for_bw)

        aot_graphs_log.info(format_graph_code(f"====== Forward graph {aot_config.aot_id} ======\n", fw_module))
        aot_graphs_log.info(format_graph_code(f"====== Backward graph {aot_config.aot_id} ======\n", bw_module))

        with track_graph_compiling(aot_config, "forward"):
            compiled_fw_func = aot_config.fw_compiler(
                fw_module, flat_args
            )

    class CompiledFunction(torch.autograd.Function):
        compiled_fw = compiled_fw_func
        compiled_bw = None
        metadata = fw_metadata
        num_symints_saved_for_bw = _num_symints_saved_for_bw

        @staticmethod
        def forward(ctx, *deduped_flat_tensor_args):

            # There is a pretty complicated calling convention around what the compiled fw returns.
            # The full list of outputs and their relative order is:
            # (*mutated_inputs, *fw_outs, *fw_intermediate_bases, *saved_tensors, *saved_symints)
            # - Note that in the synthetic bases case, mutated_inputs will correspond to an updated version
            #   of the original view, and not the synthetic base
            fw_outs = call_func_with_args(
                CompiledFunction.compiled_fw,
                deduped_flat_tensor_args,
                disable_amp=disable_amp,
            )

            num_outputs = CompiledFunction.metadata.num_outputs
            num_outputs_aliased_to_inputs = (
                CompiledFunction.metadata.num_outputs_aliased_to_inputs
            )
            num_outputs_aliased_to_intermediates = (
                CompiledFunction.metadata.num_outputs_aliased_to_intermediates
            )
            num_outputs_aliased = CompiledFunction.metadata.num_outputs_aliased
            num_intermediate_bases = CompiledFunction.metadata.num_intermediate_bases
            num_symints_saved_for_bw = CompiledFunction.num_symints_saved_for_bw
            num_mutated_inputs = CompiledFunction.metadata.num_mutated_inputs
            num_mutated_metadata_only_inputs = (
                CompiledFunction.metadata.num_mutated_metadata_only_inputs
            )
            # Our forward() returns both (mutated_inputs, outputs, output_intermediate_bases, saved_tensors, saved_symints)
            num_forward_returns = num_mutated_inputs + num_outputs + num_intermediate_bases

            assert num_forward_returns == len(
                CompiledFunction.metadata.requires_grad_info
            ) + num_intermediate_bases

            # Partitioners must put symint arguments at the end separate from tensor arguments
            if num_symints_saved_for_bw > 0:
                tensors_saved_for_backwards = fw_outs[
                    num_forward_returns:-num_symints_saved_for_bw
                ]
                assert all(
                    [isinstance(x, torch.Tensor) for x in tensors_saved_for_backwards]
                )
                # See Note [Detaching saved tensors in AOTAutograd]
                ctx.save_for_backward(*(x.detach() if x._is_view() else x for x in tensors_saved_for_backwards))
                symint_outs = fw_outs[-num_symints_saved_for_bw:]
                assert all(
                    [
                        isinstance(x, (int, float, torch.SymInt, torch.SymFloat))
                        for x in symint_outs
                    ]
                )
                ctx.symints = symint_outs
            else:
                tensors_saved_for_backwards = fw_outs[num_forward_returns:]
                # See Note [Detaching saved tensors in AOTAutograd]
                ctx.save_for_backward(*(x.detach() if x._is_view() else x for x in tensors_saved_for_backwards))
                ctx.symints = []

            raw_returns = fw_outs[0:num_forward_returns]

            # Wrap all autograd.Function.forward() outputs that are aliases
            # so that autograd.Function doesn't treat them as tensors
            if num_mutated_metadata_only_inputs > 0:
                for i, idx in enumerate(
                    CompiledFunction.metadata.mutated_inp_indices
                ):
                    # We could make this faster by only looping over inputs with metadata-only mutations
                    # (instead of looping over inputs with either data or metadata mutations), but there shouldn't be many.
                    info = CompiledFunction.metadata.input_info[idx]
                    if info.mutates_metadata and not info.mutates_data:
                        raw_returns[i] = TensorAlias(raw_returns[i])

                if config.debug_assert:
                    user_mutated_inputs_raw = raw_returns[0:num_mutated_inputs]
                    mut_inp_infos = [
                        x for x in CompiledFunction.metadata.input_info if x.mutates_data or x.mutates_metadata
                    ]
                    assert len(user_mutated_inputs_raw) == len(mut_inp_infos)

            if num_outputs_aliased > 0:
                for idx in CompiledFunction.metadata.aliased_out_indices:
                    raw_return_idx = num_mutated_inputs + idx
                    raw_returns[raw_return_idx] = TensorAlias(raw_returns[raw_return_idx])

                if config.debug_assert:
                    intermediates_raw = raw_returns[num_mutated_inputs + num_outputs:]
                    assert not any(isinstance(x, TensorAlias) for x in intermediates_raw)

            # invariant: intermediate bases always require gradients, so we don't have to
            # consider marking them as non-differentiable.
            raw_returns_not_including_intermediate_bases = raw_returns[:num_mutated_inputs + num_outputs]
            fw_outs_not_requiring_grad = [
                x
                for (i, x) in enumerate(raw_returns_not_including_intermediate_bases)
                if isinstance(x, torch.Tensor)
                and not CompiledFunction.metadata.requires_grad_info[i]
            ]
            ctx.mark_non_differentiable(*fw_outs_not_requiring_grad)

            return tuple(raw_returns)

        @staticmethod
        def backward(ctx, *flat_args):
            # Calling convention: we expect a grad_out passed to the backward:
            # - for every output of the fw that does *not* alias an input or graph intermediate
            # - for every updated_input generated by the fw that does *not* alias an input (aka only data-mutations)
            # - for every graph intermediate that we need to use to generate an output later.
            # The other outputs in the autograd.Function.forward that do *not* show up in the backward include:
            # - outputs that alias inputs or graph intermediates
            # - updated inputs due to metadata-only mutations.
            # We need to return them in the forward, but ensure that they all do not get gradients in the backward,
            # and we filter them out here before passing the remaining grad_outputs into the compiled backward.
            num_mutated_inps = CompiledFunction.metadata.num_mutated_inputs
            num_intermediate_bases = CompiledFunction.metadata.num_intermediate_bases
            expected_grad_outs = (
                CompiledFunction.metadata.num_outputs + num_mutated_inps + num_intermediate_bases
            )

            assert len(flat_args) == expected_grad_outs
            out_info = CompiledFunction.metadata.output_info
            if (
                CompiledFunction.metadata.num_mutated_metadata_only_inputs > 0
                or CompiledFunction.metadata.num_outputs_aliased > 0
            ):
                inp_tangents, out_tangents, intermediate_base_tangents = (
                    flat_args[0:num_mutated_inps],
                    flat_args[num_mutated_inps:num_mutated_inps + CompiledFunction.metadata.num_outputs],
                    flat_args[num_mutated_inps + CompiledFunction.metadata.num_outputs:],
                )
                # input_info contains info on *every* input,
                # But in the backward(), we are only given grad outputs for every mutated input.
                # We then need to filter out the grad outputs that correspond to metadata-only mutations.
                mutated_inp_indices = CompiledFunction.metadata.mutated_inp_indices
                input_info = CompiledFunction.metadata.input_info
                assert len(inp_tangents) == len(mutated_inp_indices)
                inp_tangents_filtered = [
                    x
                    for x, info_idx in zip(inp_tangents, mutated_inp_indices)
                    if input_info[info_idx].mutates_data
                ]
                # We also need to filter out grad outputs that correspond to outputs aliasing inputs/intermediates
                out_tangents_filtered = [
                    x
                    for x, info in zip(out_tangents, out_info)
                    if (info.output_type == OutputType.non_alias or info.output_type == OutputType.unsafe_view_alias)
                    and issubclass(info.raw_type, torch.Tensor)
                ]
                # intermediate bases always require gradients, and always participate in the backward graph.
                flat_bw_args = itertools.chain(inp_tangents_filtered, out_tangents_filtered, intermediate_base_tangents)

                # sanity asserts
                # metadata_only_inps = [
                #     x for x, info_idx in zip(inp_tangents, mutated_inp_indices)
                #     if not input_info[info_idx].mutates_data
                # ]
                # aliased_outputs = [
                #     x for x, info in zip(out_tangents, out_info) if info.output_type != OutputType.non_alias]
                # assert all(x is None for x in metadata_only_inps)
                # assert all(x is None for x in aliased_outputs)
            else:
                # filter out non-tensor grad_outputs (aka due to ints being returned as outputs in the forward)
                num_mutated_inps = CompiledFunction.metadata.num_mutated_inputs
                mutated_inp_args = flat_args[:num_mutated_inps] if num_mutated_inps > 0 else []
                user_tangents = flat_args[num_mutated_inps:]
                assert len(user_tangents) == len(out_info)
                filtered_user_tangents = [x for x, info in zip(user_tangents, out_info) if issubclass(info.raw_type, torch.Tensor)]
                flat_bw_args = tuple(mutated_inp_args) + tuple(filtered_user_tangents)

            contiguous_args = [
                t.contiguous() if torch.is_tensor(t) else t for t in flat_bw_args
            ]

            all_args = (
                list(ctx.symints) + list(ctx.saved_tensors) + list(contiguous_args)
            )
            del contiguous_args

            def call_compiled_backward():
                if CompiledFunction.compiled_bw is None:
                    assert all(a is not None for a in all_args)
                    if aot_config.dynamic_shapes:
                        all_args_list = list(all_args)
                        CompiledFunction.compiled_bw = create_aot_dispatcher_function(
                            bw_module, all_args_list, AOTConfig(
                                aot_config.bw_compiler, None, None,
                                aot_config.decompositions, 0, aot_config.aot_id,
                                aot_config.keep_inference_input_mutations,
                                aot_config.dynamic_shapes,
                                inference_compiler=None,
                                aot_autograd_arg_pos_to_source=None,
                            )
                        )
                    else:
                        context = disable_autocast_manager if disable_amp else nullcontext
                        with context(), track_graph_compiling(aot_config, "backward"):
                            CompiledFunction.compiled_bw = aot_config.bw_compiler(
                                bw_module, all_args
                            )

                ctx.maybe_clear_saved_tensors()
                out = call_func_with_args(
                    CompiledFunction.compiled_bw,
                    all_args,
                    steal_args=True,
                    disable_amp=disable_amp,
                )

                return tuple(out)

            if torch.is_grad_enabled() and any(t.requires_grad for t in all_args if isinstance(t, torch.Tensor)):
                # Ensure that the graph is connected, and error if double backward is performed.
                # See comment for why once_differentiable is not sufficient:
                # https://github.com/pytorch/pytorch/pull/92348/files#r1072962107
                class CompiledFunctionBackward(torch.autograd.Function):
                    @staticmethod
                    def forward(ctx, *unused_args):
                        return call_compiled_backward()

                    @staticmethod
                    def backward(ctx, *args):
                        raise RuntimeError("torch.compile with aot_autograd does not currently support double backward")
                # Pass args even though they're unused, so that the graph is built
                out = CompiledFunctionBackward.apply(*all_args)
            else:
                out = call_compiled_backward()
            return out

    compiled_function = create_runtime_wrapper(
        CompiledFunction.apply,
        runtime_metadata=fw_metadata,
        trace_joint=True,
        keep_input_mutations=False,
        disable_amp=disable_amp
    )

    if not config.debug_assert:
        return compiled_function

    flat_requires_grad = [
        a.requires_grad if isinstance(a, Tensor) else None for a in flat_args
    ]

    @wraps(compiled_function)
    def debug_compiled_function(*args):
        # TODO: Check aliasing relationships
        # TODO: Check strides for metadata mutation
        # (NB: ideally, this logic is factored out of this function and
        # you move these debug checks there)

        # Check requires grad.  Bad case is when we compiled with
        # requires_grad = False, but input requires_grad = True
        # (vice versa is OK; we compute a gradient and then throw
        # it away when it hits the input.)
        for i, a in enumerate(args):
            can_require_grad = flat_requires_grad[i]
            if can_require_grad is None:
                assert not isinstance(a, Tensor)
            elif not can_require_grad:
                assert not a.requires_grad, format_guard_bug_msg(
                    aot_config,
                    f"{describe_input(i, aot_config)} would not require grad",
                )

        return compiled_function(*args)

    return debug_compiled_function


@dynamo_timed
def create_aot_dispatcher_function(
    flat_fn, flat_args: List[Any], aot_config: AOTConfig
):
    """
    Traces the forward and backward graphs of the attr:`flat_fn` to generate a
    joint graph. The joint graph is an Fx graph with Aten ops. Please refer to
    the tracing mechanism to understand the graph capturing details.

    The joint graph is then passed through attr:`partition_fn` to isolate the
    forward and backward portions, which are then respectively compiled via the
    provided attr:`fw_compiler` and attr:`bw_compiler`.

    The resulting compiled forward and backward graphs are then wrapped up in a
    ``torch.autograd.Function`` object.

    The calling convention here is that the first aot_config.num_params_buffers
    inputs in flat_args are parameters and buffers, and the rest are inputs.

    We use this to assume that parameters/buffer's shapes don't change.
    """

    # This is the main entry point.
    # TODO: Chillee argues that dynamo itself should pass in fake tensors to
    # the list of arguments when compiling; at the moment we do not do this

    if aot_config.decompositions is None:
        aot_config.decompositions = {}

    aot_config.decompositions = {
        **aot_autograd_decompositions,
        **aot_config.decompositions,
    }

    # NB: don't bother setting allow_fallback_kernels; this should not actually
    # be configurable in fake tensor, we should automatically do the right
    # thing
    if config.debug_fake_cross_ref:
        # This is a little messy but TorchDynamo directly changes `use_fake_tensor`
        # so it's not enough for user to change the config manually
        # TODO: have TorchDynamo read in `use_fake_tensor` from os environ /
        # coordinate flags
        config.use_fake_tensor = False

    # Check flat_args to see if they're already fake.  If so, use that fake
    # mode instead.

    for x in flat_args:
        if isinstance(x, FakeTensor):
            fake_mode = x.fake_mode
            shape_env = fake_mode.shape_env
            break
    else:
        shape_env = ShapeEnv() if aot_config.dynamic_shapes else None
        fake_mode = (
            FakeTensorMode(shape_env=shape_env)
            if config.use_fake_tensor
            else nullcontext()
        )

    cross_ref = CrossRefFakeMode() if config.debug_fake_cross_ref else nullcontext()
    python_dispatcher_mode = (
        enable_python_dispatcher() if shape_env is not None else nullcontext()
    )

    with torch.autograd.set_multithreading_enabled(
        False
    ), preserve_rng_state(), cross_ref, fake_mode, python_dispatcher_mode:

        def process_inputs(flat_args):
            if config.use_fake_tensor or isinstance(fake_mode, FakeTensorMode):
                def convert(idx, x):
                    if shape_env is not None:
                        from torch._dynamo.source import ConstantSource
                        if isinstance(x, int):
                            return shape_env.create_symintnode(
                                shape_env.create_symbol(x, ConstantSource(f"sym_{idx}")),
                                hint=x
                            )
                    if not isinstance(x, torch.Tensor):
                        return x
                    if isinstance(x, FakeTensor):
                        assert x.fake_mode is fake_mode
                        return x
                    # TODO: Ensure that this codepath is never exercised from
                    # Dynamo
                    if (
                        idx < aot_config.num_params_buffers
                        and config.static_weight_shapes
                    ):
                        return fake_mode.from_tensor(x, static_shapes=True)
                    return fake_mode.from_tensor(x, static_shapes=False)

                return [convert(idx, x) for idx, x in enumerate(flat_args)]
            else:
                return flat_args

        fake_flat_args = process_inputs(flat_args)

        needs_autograd = (
            any([x.requires_grad for x in fake_flat_args if isinstance(x, Tensor)])
            and torch.is_grad_enabled()
        )
        with enable_python_dispatcher():
            fw_metadata = run_functionalized_fw_and_collect_metadata(
                flat_fn,
                keep_input_mutations=aot_config.keep_inference_input_mutations and not needs_autograd,
            )(*fake_flat_args)

        # crappy version of dispatcher
        # TODO: Do this properly
        if needs_autograd:
            compiler_fn = aot_dispatch_autograd
        else:
            compiler_fn = aot_dispatch_base

        compiler_fn = partial(aot_wrapper_synthetic_base, compiler_fn=compiler_fn, needs_autograd=needs_autograd)
        compiler_fn = partial(aot_wrapper_dedupe, compiler_fn=compiler_fn)
        # You can put more passes here

        compiled_fn = compiler_fn(flat_fn, fake_flat_args, aot_config, fw_metadata=fw_metadata)

        if not hasattr(compiled_fn, "_boxed_call"):
            compiled_fn = make_boxed_func(compiled_fn)

        return compiled_fn


# Inspired by autodidax (thanks!)
class PytreeThunk:
    spec = None
    # These are some kinda dumb microoptimizations that save about 3-4 us of overhead.
    is_simple = (
        None  # if the output spec is a tuple/list, we won't bother unflattening it.
    )
    is_really_simple = None  # if the output spec is a LeafSpec

    def set(self, spec):
        assert self.spec is None or self.spec == spec
        self.spec = spec
        if type(self.spec) in [tuple, list] and all(
            isinstance(i, pytree.LeafSpec) for i in spec.children_specs
        ):
            self.is_simple = True
        if isinstance(self.spec, pytree.LeafSpec):
            self.is_really_simple = True

    def unflatten(self, x):
        if self.is_really_simple:
            return x[0]
        if self.is_simple:
            return x
        return pytree.tree_unflatten(x, self.spec)


def aot_function(
    fn: Callable,
    fw_compiler: Callable,
    bw_compiler: Optional[Callable] = None,
    partition_fn: Callable = default_partition,
    decompositions: Optional[Dict] = None,
    num_params_buffers: int = 0,
    hasher_type=None,  # deprecated
    static_argnums: Optional[Tuple[int]] = None,  # deprecated
    keep_inference_input_mutations: bool = False,
    inference_compiler: Optional[Callable] = None,
    *,
    # Whether or not to trace with dynamic shapes
    dynamic=False,
) -> Callable:
    """
    Traces the forward and backward graph of :attr:`fn` using torch dispatch
    mechanism, and then compiles the generated forward and backward graphs
    through :attr:`fw_compiler` and :attr:`bw_compiler`.

    :func:`aot_function` traces the forward and backward graph ahead of time,
    and generates a joint forward and backward graph.  :attr:`partition_fn` is
    then used to separate out forward and backward graphs. The partitioner
    function can be used to perform optimizations such as recomputation. One can
    set `decompositions` dictionary to decompose the operators into a sequence
    of core or simpler operators supported by the backend compilers.

    .. warning::
        This API is experimental and likely to change.

    Args:
        fn (Callable): A Python function that takes one ore more arguments. Must
            return one or more Tensors.
        fw_compiler (Callable): A Python function that accepts an Fx graph with
            Aten ops and input args, and returns a Callable that semantically is
            equivalent to the input Fx graph.
        bw_compiler (Optional[Callable]): A Python function that accepts an
            Fx graph with Aten ops and input args, and returns a Callable that
            semantically is equivalent to the input Fx graph.  Default: None
            (when None, it defaults to the :attr:`fw_compiler`)
        partition_fn (Callable): A Python function that takes a joint forward
            and backward graph, and partitions it into separate forward and
            backward graphs.
        decompositions (Dict): A dictionary to define the decomposition of
            larger Aten ops into simpler or core Aten ops.
        inference_compiler (Optional[Callable]): A Python function that accepts an
            Fx graph with Aten ops and input args, and returns a Callable that
            semantically is equivalent to the input Fx graph. inference_compiler is invoked
            if no autograd is needed. Default: None
            (when None, it defaults to the :attr:`fw_compiler`)
    Returns:
        Returns a ``Callable`` that retains the eager behavior of the original
        :attr:`fn`, but with forward and backward graph compiled via
        :attr:`fw_compile` and :attr:`bw_compile`.

    A simple example usage of :func:`aot_function` is as follows. This example
    will print the forward and backward graphs of the function ``fn``

        >>> fn = lambda x : x.sin().cos()
        >>> def print_compile_fn(fx_module, args):
        >>>     print(fx_module)
        >>>     return fx_module
        >>> aot_fn = aot_function(fn, print_compile_fn)
        >>> x = torch.randn(4, 5, requires_grad=True)
        >>> aot_fn(x)
    """
    if static_argnums is not None:
        raise RuntimeError(
            "static_argnums has been deprecated - manually wrap your function or use torchdynamo."
        )

    if bw_compiler is None:
        bw_compiler = fw_compiler
    if inference_compiler is None:
        inference_compiler = fw_compiler
    aot_config = AOTConfig(
        fw_compiler=fw_compiler,
        bw_compiler=bw_compiler,
        inference_compiler=fw_compiler,
        partition_fn=partition_fn,
        decompositions=decompositions,
        num_params_buffers=num_params_buffers,
        aot_id=next(AOT_COUNTER),
        keep_inference_input_mutations=keep_inference_input_mutations,
        dynamic_shapes=dynamic,
        aot_autograd_arg_pos_to_source=None,
    )
    cached_res = None

    @wraps(fn)
    def returned_function(*args, **kwargs):
        nonlocal cached_res
        # Now flatten the tensor args
        flat_args, _ = pytree.tree_flatten((args, kwargs))

        # Compile the function and save it in the cache
        if cached_res is None:
            # Save the args_spec for flat_tensor_args to unflatten while tracing
            _, tensor_args_spec = pytree.tree_flatten((args, kwargs))
            out_spec = PytreeThunk()

            def flat_fn(*flat_args):
                # The input are flattened tensor args. Prepare the args in the
                # order that original function expects. Add static args as well.
                # They will appear as tensor constants in the traced graph.
                nonlocal out_spec
                args, kwargs = pytree.tree_unflatten(flat_args, tensor_args_spec)
                tree_out = fn(*args, **kwargs)
                flat_out, spec = pytree.tree_flatten(tree_out)
                for i in flat_out:
                    is_known_type = False
                    for j in KNOWN_TYPES:
                        if isinstance(i, j):
                            is_known_type = True
                            break
                    if not is_known_type:
                        raise RuntimeError(
                            f"Found {type(i)} in output, which is not a known type. "
                            "If this type holds tensors, you need to register a pytree for it. "
                            "See https://github.com/pytorch/functorch/issues/475 for a brief "
                            "explanation why. If you don't need to register a pytree, please "
                            "leave a comment explaining your use case and we'll make this more "
                            "ergonomic to deal with"
                        )
                out_spec.set(spec)
                return flat_out

            compiled_fn = create_aot_dispatcher_function(
                flat_fn,
                flat_args,
                aot_config,
            )
            cached_res = (compiled_fn, out_spec)

        cached_fn, out_spec = cached_res
        out = cached_fn(flat_args)
        return out_spec.unflatten(out)

    return returned_function


def aot_module(mod: nn.Module, *args, **kwargs) -> nn.Module:
    """
    Traces the forward and backward graph of :attr:`mod` using torch dispatch
    tracing mechanism. It is wrapper function, that underneath uses
    :func:`aot_function` to perform tracing and compilation.

    :func:`aot_module` lifts the parameters and buffers of ``nn.Module`` as inputs
    to a new callable which is then compiled through :func:`aot_function`.

    .. warning::
        This API is experimental and likely to change.

    Args:
        mod (Callable): A ``nn.Module`` module.
        args : args to be passed to :func:`aot_function`
        kwargs : kwargs to be passed to :func:`aot_function`

    Returns:
        Returns a ``nn.Module`` that retains the eager behavior of the original
        :attr:`mod`, but with forward and backward graph compiled.

    """
    # See Note: [Fake Modules and AOTAutograd]
    torch._dynamo.utils.assert_no_fake_params_or_buffers(mod)

    def functional_call(named_params, named_buffers, *args, **kwargs):
        params_and_buffers = {**named_params, **named_buffers}
        return torch.func.functional_call(mod, params_and_buffers, args, kwargs)

    named_params = dict(mod.named_parameters(remove_duplicate=False))
    named_buffers = dict(mod.named_buffers(remove_duplicate=False))
    num_params_buffers = len(named_params) + len(named_buffers)
    compiled_f = aot_function(
        functional_call, num_params_buffers=num_params_buffers, *args, **kwargs
    )

    class AOTModule(nn.Module):
        def __init__(self):
            super().__init__()
            self.orig_module = mod

        def forward(self, *args, **kwargs):
            return compiled_f(
                named_params,
                named_buffers,
                *args,
                **kwargs,
            )

    return AOTModule()


def aot_module_simplified(
    mod: nn.Module,
    args,
    fw_compiler: Callable,
    bw_compiler: Optional[Callable] = None,
    partition_fn: Callable = default_partition,
    decompositions: Optional[Dict] = None,
    hasher_type=None,
    static_argnums=None,
    keep_inference_input_mutations=False,
    inference_compiler: Optional[Callable] = None,
) -> nn.Module:
    """
    This is the simplified or low overhead version of aot_module. For frontends
    like TorchDynamo, the input functions/modules to AOT are static and have
    unpacked inputs/outputs. This gives us an opportunity to remove the
        (1) pytree overhead to parse inputs/outputs,
        (2) AOT Autograd cache,
        (3) Reading of params/buffers in every forward call

    :func:`aot_module_simplified` removes these overheads.
    """
    #########################################################

    # Redudant with dynamo, but worth having in case this gets invoked elsewhere.

    # Note [Fake Modules and AOTAutograd]
    #
    # A simple heuristic for when to use fake versus real tensors is that fake tensors are for compile time
    # (when we don't want to actually run the compute, but we do want to know about metadata),
    # and real tensors are for runtime (when we actually want to do the compute.) However, in AOTAutograd,
    # modules are the exception: we always pass AOTAutograd modules with real tensors.
    # This is because AOTAutograd will produce a compiled function which needs to directly access any
    # parameters the compiled function may need, but these parameters will NOT be passed in by the caller (aka Dynamo).
    # So at compile time, the compiled function we produce must close over any parameters, and those parameters must be
    # real parameters, and we cannot do this unless at compile time we get a module with real tensors.

    # Even if Dynamo did pass all parameters explicitly at runtime, which would eliminate the need to close over
    # the parameters, it would still be profitable to pass real tensor parameters to the compiler at compile time,
    # because some compilation strategies like CUDA graphs want to burn in the pointer addresses where the parameter data live,
    # and of course we can't do that unless we give the backend a real tensor.
    torch._dynamo.utils.assert_no_fake_params_or_buffers(mod)

    params = {
        **dict(mod.named_parameters(remove_duplicate=False)),
        **dict(mod.named_buffers(remove_duplicate=False)),
    }
    params_flat, params_spec = pytree.tree_flatten(params)
    params_flat = tuple(params_flat)
    params_len = len(params_flat)

    def functional_call(*args, **kwargs):
        with stateless._reparametrize_module(
            mod, pytree.tree_unflatten(args[:params_len], params_spec)
        ):
            if isinstance(mod, torch.fx.GraphModule):
                with fx_traceback.preserve_node_meta(), warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore", "Anomaly Detection has been enabled."
                    )
                    with torch.autograd.detect_anomaly(check_nan=False):
                        out = Interpreter(mod).run(*args[params_len:], **kwargs)
            else:
                out = mod(*args[params_len:], **kwargs)

        if not isinstance(out, (tuple, list)):
            raise RuntimeError(
                "Graph output must be a tuple(). This is so that we can avoid "
                "pytree processing of the ouputs. Please change the module to "
                "have tuple outputs or use aot_module instead."
            )
        return out

    assert static_argnums is None
    if bw_compiler is None:
        bw_compiler = fw_compiler
    if inference_compiler is None:
        inference_compiler = fw_compiler

    full_args = []
    # First, the params
    full_args.extend(params_flat)

    aot_autograd_arg_pos_to_source = None
    # Then, the params 1:1 mapped sources, if relevant.
    if hasattr(mod, "_param_name_to_source"):
        aot_autograd_arg_pos_to_source = []
        # We now know this came from dynamo, and (1) we care about guards,
        # so setting up aot_autograd_arg_pos_to_source for downstream dedup guards
        # can now be done safely. (2) Dynamo logic protects the 1:1 sizing below.
        for name in params.keys():
            assert name in mod._param_name_to_source, f"{name} not found."
            aot_autograd_arg_pos_to_source.append(mod._param_name_to_source[name])

    # Next, the input args
    full_args.extend(args)

    if hasattr(mod, "graph"):
        # Non dynamo entrypoints can get to here...
        for i, node in enumerate(mod.graph.nodes):
            if node.op == "placeholder":
                if hasattr(node, "_dynamo_source"):
                    # ... but not here!
                    if aot_autograd_arg_pos_to_source is None:
                        aot_autograd_arg_pos_to_source = []
                    aot_autograd_arg_pos_to_source.append(node._dynamo_source)

    if aot_autograd_arg_pos_to_source is not None:
        assert len(full_args) == len(aot_autograd_arg_pos_to_source)

    dynamic_shapes = False
    for x in full_args:
        if isinstance(x, FakeTensor):
            dynamic_shapes = x.fake_mode.shape_env is not None
            break

    aot_config = AOTConfig(
        fw_compiler=fw_compiler,
        bw_compiler=bw_compiler,
        inference_compiler=inference_compiler,
        partition_fn=partition_fn,
        decompositions=decompositions,
        num_params_buffers=params_len,
        aot_id=next(AOT_COUNTER),
        keep_inference_input_mutations=keep_inference_input_mutations,
        dynamic_shapes=dynamic_shapes,
        aot_autograd_arg_pos_to_source=aot_autograd_arg_pos_to_source
    )

    compiled_fn = create_aot_dispatcher_function(
        functional_call,
        full_args,
        aot_config,
    )

    # TODO: There is something deeply wrong here; compiled_fn running with
    # the boxed calling convention, but aot_module_simplified somehow
    # historically returned a function that was not the boxed calling
    # convention.  This should get fixed...
    def forward(*runtime_args):
        full_args = []
        full_args.extend(params_flat)
        full_args.extend(runtime_args)
        return compiled_fn(full_args)

    # Just for convenience
    forward.zero_grad = mod.zero_grad
    forward.named_parameters = mod.named_parameters
    forward.named_buffers = mod.named_buffers

    return forward


compiled_function = aot_function
compiled_module = aot_module
