# Copyright (c) 2023-2024 Advanced Micro Devices, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# * Neither the name of qonnx nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import clize
import dataclasses as dc
import itertools
import numpy as np
import pprint
from warnings import warn

from qonnx.core.modelwrapper import ModelWrapper
from qonnx.core.onnx_exec import execute_node
from qonnx.transformation.fold_constants import FoldConstants
from qonnx.transformation.gemm_to_matmul import GemmToMatMul
from qonnx.transformation.infer_datatypes import InferDataTypes
from qonnx.transformation.infer_shapes import InferShapes
from qonnx.transformation.lower_convs_to_matmul import LowerConvsToMatMul
from qonnx.util.cleanup import cleanup_model
from qonnx.util.onnx import valueinfo_to_tensor

# walk the graph to deduce range information about each tensor
# assumptions:
# - layout and shape inference already completed
# - range info is generated per-element (broadcasted to this shape even if identical entries)


# RangeInfo dataclass: we will use instances of this to represent the range information for tensors
@dc.dataclass
class RangeInfo:
    # the range encountered in practice when observing tensors during inference
    range: tuple = None
    # (optional) the underlying integer range for tensor, if applicable
    # if this is set, so are scale and bias, to satisfy:
    # range = scale * int_range + bias
    int_range: tuple = None
    # (optional) the scaling factor applied to the integer range, if applicable
    scale: np.ndarray = None
    # (optional) the bias applied after the scaling, if applicable
    bias: np.ndarray = None
    # whether this particular range is always fixed (due to its tensor having an initializer)
    is_initializer: bool = False

    def has_integer_info(self) -> bool:
        # whether the RangeInfo has its int_range, scale and bias populated
        integer_props = [self.int_range, self.scale, self.bias]
        return all([x is not None for x in integer_props])


def is_dyn_input(x, model):
    # return True if a given tensor has no initializer (=dynamic), False otherwise
    return model.get_initializer(x) is None and x != ""


def promote_range_shape(tensor_range: tuple, tensor_vi):
    # ensure the range has the apropriate (per-element shape)
    # i.e. range = (range_min, range_max) where range_min and
    # range_max have the same shape as the original tensor
    proto_tensor = valueinfo_to_tensor(tensor_vi)
    tensor_shape = proto_tensor.shape
    if isinstance(tensor_range[0], np.ndarray) and tensor_range[0].shape == tensor_shape:
        return tensor_range
    else:
        # fix shape using numpy broadcasting
        range_min = tensor_range[0] + np.zeros_like(proto_tensor)
        range_max = tensor_range[1] + np.zeros_like(proto_tensor)
        return (range_min, range_max)


# range computation for monotonic functions:
# suppose we have a layer z = f(x,y) taking in two inputs x and y, outputting z
# suppose that these have ranges x = (xmin, xmax), y = (ymin, ymax), z = (zmin, zmax)
# say we have access to the input ranges, and want to find the output range
# a monotonic function will have the property that the inputs that trigger zmin and zmax
# can be found at the "corners" of the input space. so we evaluate the function at all
# possible corners of the input space:
# c0 = f(xmin, ymin)
# c1 = f(xmax, ymin)
# c2 = f(xmin, ymax)
# c3 = f(xmax, ymax)
# now, we can find our output range by taking the min/max of these corners
# zmin = min(c0, c1, c2, c3)
# zmax = max(c0, c1, c2, c3)
def calc_monotonic_range(node, model, range_dict):
    opset_version = model.model.opset_import[0].version
    oname = node.output[0]
    dyn_inps = [x for x in node.input if is_dyn_input(x, model)]
    n_dyn_inp = len(dyn_inps)
    # create context for single-node execution
    ctx = {x: model.get_initializer(x) for x in node.input}
    for oname in node.output:
        ctx[oname] = valueinfo_to_tensor(model.get_tensor_valueinfo(oname))
    if n_dyn_inp == 0:
        # special case: all inputs were constants (e.g. quantized for trained weights)
        # so there is no proto vectors to operate over really - just need a single eval
        execute_node(node, ctx, model.graph, opset_version=opset_version)
        # grab new output and keep the entire thing as the range
        for oname in node.output:
            range_dict[oname].range = (ctx[oname], ctx[oname])
            range_dict[oname].is_initializer = True
        return
    # going beyond this point we are sure we have at least one dynamic input
    # generate min-max prototype vectors for each dynamic input
    proto_vectors = []
    for inp in dyn_inps:
        irange = range_dict[inp].range
        inp_vi = model.get_tensor_valueinfo(inp)
        proto_vectors.append(promote_range_shape(irange, inp_vi))
    # process all combinations of prototype vectors for dynamic inputs
    running_min = [None for i in range(len(node.output))]
    running_max = [None for i in range(len(node.output))]
    for inps in itertools.product(*proto_vectors):
        for i in range(n_dyn_inp):
            ctx[dyn_inps[i]] = inps[i]
        execute_node(node, ctx, model.graph, opset_version=opset_version)
        for oind, oname in enumerate(node.output):
            # grab new output and update running min/max
            out = ctx[oname]
            running_min[oind] = np.minimum(out, running_min[oind]) if running_min[oind] is not None else out
            running_max[oind] = np.maximum(out, running_max[oind]) if running_max[oind] is not None else out
    for oind, oname in enumerate(node.output):
        range_dict[oname].range = (running_min[oind], running_max[oind])


# fast interval matrix enclosure based on:
# Accelerating interval matrix multiplication by mixed precision arithmetic
# Ozaki et al.
# Algorithms 1 and 2, which turn are based on:
# Rump, Siegfried M. "INTLAB—interval laboratory." Developments in reliable computing.
# except no directed rounding (because numpy/Python has none)
def range_to_midpoint_radius(matrix_range):
    (matrix_min, matrix_max) = matrix_range
    midpoint = matrix_min + 0.5 * (matrix_max - matrix_min)
    radius = midpoint - matrix_min
    return (midpoint, radius)


def calc_matmul_range(range_A, range_B):
    (midpoint_A, radius_A) = range_to_midpoint_radius(range_A)
    (midpoint_B, radius_B) = range_to_midpoint_radius(range_B)
    radius = np.matmul(radius_A, np.abs(midpoint_B) + radius_B) + np.matmul(np.abs(midpoint_A), radius_B)
    out_base = np.matmul(midpoint_A, midpoint_B)
    out_max = out_base + radius
    out_min = out_base - radius
    return (out_min, out_max)


def calc_matmul_node_range(node, model, range_dict):
    range_A = range_dict[node.input[0]].range
    range_B = range_dict[node.input[1]].range
    range_dict[node.output[0]].range = calc_matmul_range(range_A, range_B)


# use inferred output datatype to calculate output ranges
def calc_range_outdtype(node, model, range_dict):
    oname = node.output[0]
    odt = model.get_tensor_datatype(oname)
    assert odt is not None, "Cannot infer %s range, dtype annotation is missing" % oname
    range_dict[oname].range = (odt.min(), odt.max())


# use initializers to mark point ranges i.e. tensor with initializer X has range (X, X)
def calc_range_all_initializers(model, range_dict):
    all_tensor_names = model.get_all_tensor_names()
    for tensor_name in all_tensor_names:
        tensor_init = model.get_initializer(tensor_name)
        if tensor_init is not None:
            range_dict[tensor_name] = RangeInfo(range=(tensor_init, tensor_init), is_initializer=True)
            # use % 1 == 0 to identify initializers with integer values
            if ((tensor_init % 1) == 0).all():
                range_dict[tensor_name].int_range = (tensor_init, tensor_init)
                range_dict[tensor_name].scale = np.asarray([1.0], dtype=np.float32)
                range_dict[tensor_name].bias = np.asarray([0.0], dtype=np.float32)


optype_to_range_calc = {
    "Transpose": calc_monotonic_range,
    "MatMul": calc_matmul_node_range,
    "QuantMaxNorm": calc_range_outdtype,
    "Flatten": calc_monotonic_range,
    "Reshape": calc_monotonic_range,
    "Quant": calc_monotonic_range,
    "BipolarQuant": calc_monotonic_range,
    "Mul": calc_monotonic_range,
    "Sub": calc_monotonic_range,
    "Div": calc_monotonic_range,
    "Add": calc_monotonic_range,
    "BatchNormalization": calc_monotonic_range,
    "Relu": calc_monotonic_range,
    "Pad": calc_monotonic_range,
    "AveragePool": calc_monotonic_range,
    "Trunc": calc_monotonic_range,
    "MaxPool": calc_monotonic_range,
    "Resize": calc_monotonic_range,
    "Upsample": calc_monotonic_range,
    "GlobalAveragePool": calc_monotonic_range,
    "QuantizeLinear": calc_monotonic_range,
    "DequantizeLinear": calc_monotonic_range,
    "Clip": calc_monotonic_range,
    "Sigmoid": calc_monotonic_range,
    "Concat": calc_monotonic_range,
    "Split": calc_monotonic_range,
    "Im2Col": calc_monotonic_range,
}

REPORT_MODE_RANGE = "range"
REPORT_MODE_STUCKCHANNEL = "stuck_channel"
REPORT_MODE_ZEROSTUCKCHANNEL = "zerostuck_channel"

report_modes = {REPORT_MODE_RANGE, REPORT_MODE_STUCKCHANNEL, REPORT_MODE_ZEROSTUCKCHANNEL}

report_mode_options = clize.parameters.mapped(
    [
        (REPORT_MODE_RANGE, [REPORT_MODE_RANGE], "Report ranges"),
        (REPORT_MODE_STUCKCHANNEL, [REPORT_MODE_STUCKCHANNEL], "Report stuck channels"),
        (REPORT_MODE_ZEROSTUCKCHANNEL, [REPORT_MODE_ZEROSTUCKCHANNEL], "Report 0-stuck channels"),
    ]
)


def range_analysis(
    model_filename_or_wrapper,
    *,
    irange="",
    key_filter: str = "",
    save_modified_model: str = "",
    report_mode: report_mode_options = REPORT_MODE_STUCKCHANNEL,
    lower_ops=False,
    prettyprint=False,
    do_cleanup=False,
    strip_initializers_from_report=True,
):
    assert report_mode in report_modes, "Unrecognized report_mode, must be " + str(report_modes)
    if isinstance(model_filename_or_wrapper, ModelWrapper):
        model = model_filename_or_wrapper
    else:
        model = ModelWrapper(model_filename_or_wrapper)
    if isinstance(irange, str):
        if irange == "":
            range_min = None
            range_max = None
        else:
            irange = eval(irange)
            range_min, range_max = irange
            if isinstance(range_min, list):
                range_min = np.asarray(range_min, dtype=np.float32)
            if isinstance(range_max, list):
                range_max = np.asarray(range_max, dtype=np.float32)
    elif isinstance(irange, tuple):
        range_min, range_max = irange
    elif isinstance(irange, RangeInfo):
        pass
    else:
        assert False, "Unknown irange type"
    if do_cleanup:
        model = cleanup_model(model, preserve_qnt_ops=False)
    if lower_ops:
        model = model.transform(LowerConvsToMatMul())
        model = model.transform(GemmToMatMul())
        model = cleanup_model(model)
    # call constant folding & shape inference, this preserves weight quantizers
    # (but do not do extra full cleanup, in order to preserve node/tensor naming)
    # TODO is this redundant? remove?
    model = model.transform(InferShapes())
    model = model.transform(FoldConstants())
    model = model.transform(InferDataTypes())
    if save_modified_model != "":
        model.save(save_modified_model)
    range_dict = {}
    stuck_chans = {}

    # start by calculating/annotating range info for input tensors
    for inp in model.graph.input:
        iname = inp.name
        if isinstance(irange, RangeInfo):
            range_dict[iname] = irange
        else:
            if range_min is None or range_max is None:
                # use idt annotation
                idt = model.get_tensor_datatype(iname)
                assert idt is not None, "Could not infer irange, please specify"
                range_min = idt.min()
                range_max = idt.max()
            range_dict[iname] = RangeInfo(range=(range_min, range_max))

    # add range info for all tensors with initializers
    calc_range_all_initializers(model, range_dict)

    # now walk the graph node by node and propagate range info
    for node in model.graph.node:
        dyn_inputs = [x for x in node.input if is_dyn_input(x, model)]
        inprange_ok = all([x in range_dict.keys() for x in dyn_inputs])
        op_ok = node.op_type in optype_to_range_calc.keys()
        if inprange_ok and op_ok:
            # create entries in range_dict with RangeInfo type for all outputs
            # since range analysis functions will be assigning to the .range member of
            # this RangeInfo directly later on
            for node_out in node.output:
                range_dict[node_out] = RangeInfo()
            range_calc_fxn = optype_to_range_calc[node.op_type]
            range_calc_fxn(node, model, range_dict)
            # ensure all produced ranges are per-element
            for node_out in node.output:
                out_vi = model.get_tensor_valueinfo(node_out)
                range_dict[node_out].range = promote_range_shape(range_dict[node_out].range, out_vi)
            # TODO bring back stuck channel analysis after simplification is re-introduced
        else:
            warn("Skipping %s : inp_range? %s op_ok? (%s) %s" % (node.name, str(inprange_ok), node.op_type, str(op_ok)))

    # range dict is now complete, apply filters and formatting
    if report_mode in [REPORT_MODE_ZEROSTUCKCHANNEL, REPORT_MODE_STUCKCHANNEL]:
        ret = stuck_chans
    else:
        ret = range_dict
        if strip_initializers_from_report:
            # exclude all initializer ranges for reporting
            ret = {k: v for (k, v) in ret.items() if not v.is_initializer}

    # only keep tensors (keys) where filter appears in the name
    if key_filter != "":
        ret = {k: v for (k, v) in ret.items() if key_filter in k}
    # only keep tensors (keys) where filter appears in the name
    if key_filter != "":
        ret = {k: v for (k, v) in ret.items() if key_filter in k}

    if report_mode == REPORT_MODE_RANGE:
        # TODO convert ranges in report to regular Python lists for nicer printing
        pass
    elif report_mode == REPORT_MODE_ZEROSTUCKCHANNEL:
        # only leave channels that are stuck at zero
        # value info removed since implicitly 0
        new_ret = {}
        for tname, schans in ret.items():
            schans_only_zero = set([x[0] for x in schans if x[1] == 0])
            if len(schans_only_zero) > 0:
                new_ret[tname] = schans_only_zero
        ret = new_ret
    if prettyprint:
        ret = pprint.pformat(ret, sort_dicts=False)
    return ret


def main():
    clize.run(range_analysis)


if __name__ == "__main__":
    main()
