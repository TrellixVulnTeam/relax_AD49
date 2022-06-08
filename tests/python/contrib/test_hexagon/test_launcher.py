# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import annotations  # must import to defer parsing of annotations
import sys
import pytest
import numpy as np
from tvm.relay import testing

import tvm.testing
import tvm
from tvm import te
from tvm import relay, relax
from tvm.relay.backend import Executor, Runtime
from tvm.contrib.hexagon.session import Session
from tvm.script import relax as R, tir as T
from tvm.relax.testing import relay_translator, nn


@tvm.testing.requires_hexagon
def test_relax_conv2d(hexagon_session: Session):
    dtype = "float32"
    data = relay.var("data", relay.TensorType((1, 64, 64, 3), dtype))
    weight = relay.var("weight", relay.TensorType((5, 5, 3, 8), dtype))
    y = relay.nn.conv2d(
        data,
        weight,
        padding=(2, 2),
        kernel_size=(5, 5),
        data_layout="NHWC",
        kernel_layout="HWIO",
        out_dtype="float32",
    )
    f = relay.Function([data, weight], y)
    relay_mod = tvm.IRModule.from_expr(f)

    # target_hexagon = "llvm -keys=hexagon -link-params=0 -mattr=+hvxv69,+hvx-length128b,+hvx-qfloat,-hvx-ieee-fp -mcpu=hexagonv69 -mtriple=hexagon"
    target_hexagon = tvm.target.hexagon("v68")
    target = tvm.target.Target(target_hexagon, host=target_hexagon)
    relax_mod = relay_translator.from_relay(relay_mod["main"], target)

    R.parser.pretty_print(relax_mod)

    ex = relax.vm.build(relax_mod, target)
    dev = hexagon_session.device

    vm_mod = hexagon_session.get_executor_from_factory(ex)
    vm_rt = relax.VirtualMachine(vm_mod, dev)
    print(vm_mod)
    data = tvm.nd.array(np.random.rand(1, 64, 64, 3).astype(np.float32), dev)
    weight = tvm.nd.array(np.random.rand(5, 5, 3, 8).astype(np.float32), dev)
    res = vm_rt["main"](data, weight)


@tvm.testing.requires_hexagon
def test_relax_mlp(hexagon_session: Session):
    relay_mod, _ = testing.mlp.get_workload(batch_size=1, dtype="float32")

    target_hexagon = tvm.target.hexagon("v68")
    target = tvm.target.Target(target_hexagon, host=target_hexagon)
    relax_mod = relay_translator.from_relay(relay_mod["main"], target)

    R.parser.pretty_print(relax_mod)

    ex = relax.vm.build(relax_mod, target)
    dev = hexagon_session.device

    vm_mod = hexagon_session.get_executor_from_factory(ex)
    vm_rt = relax.VirtualMachine(vm_mod, dev)

    shape = (1, 1, 28, 28)
    data = tvm.nd.array(np.random.rand(*shape).astype(np.float32), dev)
    params = nn.init_params(relax_mod, dev)
    res = vm_rt["main"](data, *params)


def get_onnx_mobilenet():
    """Download and import mobilenet model with ONNX"""
    import onnx  # pylint: disable=import-outside-toplevel

    model_url = "https://github.com/onnx/models/raw/main/vision/classification/mobilenet/model/mobilenetv2-7.onnx"
    model_path = tvm.contrib.download.download_testdata(
        model_url, "mobilenetv2-7.onnx", module="onnx"
    )
    return onnx.load(model_path)


@tvm.testing.requires_hexagon
def test_relax_mobilenet_onnx(hexagon_session: Session):
    onnx_model = get_onnx_mobilenet()
    data_np = np.random.rand(1, 3, 224, 224).astype("float32")
    shape_dict = {"input": data_np.shape}
    relay_mod, params = relay.frontend.from_onnx(onnx_model, shape_dict, freeze_params=True)

    target_hexagon = tvm.target.hexagon("v68")
    target = tvm.target.Target(target_hexagon, host=target_hexagon)
    relax_mod = relay_translator.from_relay(relay_mod["main"], target_hexagon)
    R.parser.pretty_print(relax_mod)

    # Compile and run on Hexagon.
    ex = relax.vm.build(relax_mod, target)
    dev = hexagon_session.device

    vm_mod = hexagon_session.get_executor_from_factory(ex)
    vm_rt = relax.VirtualMachine(vm_mod, dev)
    data = tvm.nd.array(data_np, dev)
    hexagon_res = vm_rt["main"](data)

    # Compile and run on LLVM for comparison.
    relax_mod = relay_translator.from_relay(relay_mod["main"], "llvm")
    ex = relax.vm.build(relax_mod, "llvm")
    dev = tvm.cpu()
    vm_rt = relax.VirtualMachine(ex, dev)
    data = tvm.nd.array(data_np, dev)
    llvm_res = vm_rt["main"](data)
    tvm.testing.assert_allclose(hexagon_res.numpy(), llvm_res.numpy(), rtol=1e-3)


@tvm.testing.requires_hexagon
def test_relax_mobilenet_relay(hexagon_session: Session):
    relay_mod, params = testing.mobilenet.get_workload(batch_size=1, dtype="float32")
    data_np = np.random.rand(1, 3, 224, 224).astype("float32")

    target_hexagon = tvm.target.hexagon("v68")
    target = tvm.target.Target(target_hexagon, host=target_hexagon)

    # translate the relay mobilenet and bind params
    relax_mod = relay_translator.from_relay(relay_mod["main"], target, params)

    # Compile and run on Hexagon.
    ex = relax.vm.build(relax_mod, target)
    dev = hexagon_session.device

    vm_mod = hexagon_session.get_executor_from_factory(ex)
    vm_rt = relax.VirtualMachine(vm_mod, dev)
    data = tvm.nd.array(data_np, dev)
    hexagon_res = vm_rt["main"](data)

    # Compile and run on LLVM for comparison.
    relax_mod = relay_translator.from_relay(relay_mod["main"], "llvm", params)
    ex = relax.vm.build(relax_mod, "llvm")
    dev = tvm.cpu()
    vm_rt = relax.VirtualMachine(ex, dev)
    data = tvm.nd.array(data_np, dev)
    llvm_res = vm_rt["main"](data)
    tvm.testing.assert_allclose(hexagon_res.numpy(), llvm_res.numpy(), rtol=1e-3)


@tvm.testing.requires_hexagon
def test_relax_dyn_shape(hexagon_session: Session):
    @tvm.script.ir_module
    class TestDynShape:
        @T.prim_func
        def tir_matmul(x: T.handle, y: T.handle, z: T.handle) -> None:
            T.func_attr({"global_symbol": "tir_matmul"})
            m = T.var("int32")
            n = T.var("int32")
            k = T.var("int32")
            A = T.match_buffer(x, (m, n))
            B = T.match_buffer(y, (n, k))
            C = T.match_buffer(z, (m, k))

            for i, j, k in T.grid(m, k, n):
                with T.block("matmul"):
                    vi, vj, vk = T.axis.remap("SSR", [i, j, k])
                    with T.init():
                        C[vi, vj] = T.float32(0)
                    C[vi, vj] = C[vi, vj] + A[vi, vk] * B[vk, vj]

        @R.function
        def func(x: Tensor((m, n), "float32")) -> Tensor:
            gv0 = R.call_tir(tir_matmul, (x, x), (m, n), dtype="float32")
            return gv0

    relax_mod = TestDynShape
    R.parser.pretty_print(relax_mod)

    data_np = np.random.rand(16, 16).astype("float32")
    target_hexagon = tvm.target.hexagon("v68")
    target = tvm.target.Target(target_hexagon, host=target_hexagon)
    ex = relax.vm.build(relax_mod, target)
    dev = hexagon_session.device

    vm_mod = hexagon_session.get_executor_from_factory(ex)
    vm_rt = relax.VirtualMachine(vm_mod, dev)

    data = tvm.nd.array(data_np, dev)
    vm_rt.set_input("func", data)
    vm_rt["func"]()
    outputs = vm_rt.get_outputs()
    print(outputs)


# @tvm.testing.requires_hexagon
# def test_relax_dynamic(hexagon_session: Session):
#     @tvm.script.ir_module
#     class TestVMMove:
#         @R.function
#         def main(x: Tensor((3, 4), "float32")):
#             return x

#     relax_mod = TestVMMove

#     mod = TestVMMove
#     target = tvm.target.Target("llvm", host="llvm")
#     ex = relax.vm.build(mod, target)
#     vm = relax.VirtualMachine(ex, tvm.cpu())
#     data = tvm.nd.array(np.random.rand(3, 4).astype(np.float32))
#     res = vm["main"](data)


@tvm.testing.requires_hexagon
def test_relax_dyn_mobilenet(hexagon_session: Session):
    relay_mod, params = testing.mobilenet.get_workload(batch_size=relay.Any(), dtype="float32")
    # relay_mod, params = testing.mlp.get_workload(batch_size=relay.Any(), dtype="float32")
    data_np = np.random.rand(1, 3, 224, 224).astype("float32")

    target_hexagon = tvm.target.hexagon("v68")
    target = tvm.target.Target(target_hexagon, host=target_hexagon)

    # translate the relay mobilenet and bind params
    relax_mod = relay_translator.from_relay(relay_mod["main"], target, params)
    R.parser.pretty_print(relax_mod)

    # Compile and run on Hexagon.
    ex = relax.vm.build(relax_mod, target)
    dev = hexagon_session.device
    vm_mod = hexagon_session.get_executor_from_factory(ex)
    print("module loaded on hexagon")
    vm_rt = relax.VirtualMachine(vm_mod, dev)
    data = tvm.nd.array(data_np, dev)
    vm_rt.set_input("main", data)
    print("start running on hexagon")
    vm_rt["main"]()
    hexagon_res = vm_rt.get_outputs()
    print("hexagon finished")

    # Run relay vm for comparison.
    from tvm import runtime

    dev = tvm.cpu()
    data = tvm.nd.array(data_np, dev)
    target = tvm.target.Target("llvm", host="llvm")
    vm_exec = relay.vm.compile(relay_mod, target=target)
    vm_factory = runtime.vm.VirtualMachine(vm_exec, tvm.cpu())
    relay_res = vm_factory.invoke("main", data, **params)
    tvm.testing.assert_allclose(hexagon_res.numpy(), relay_res.numpy(), rtol=1e-3)


@tvm.testing.requires_hexagon
def test_add(hexagon_session: Session):
    dtype = "int8"
    A = tvm.te.placeholder((2,), dtype=dtype)
    B = tvm.te.placeholder((1,), dtype=dtype)
    C = tvm.te.compute(A.shape, lambda i: A[i] + B[0], name="C")
    sched = tvm.te.create_schedule(C.op)

    target_hexagon = tvm.target.hexagon("v68", link_params=True)
    func = tvm.build(
        sched, [A, B, C], tvm.target.Target(target_hexagon, host=target_hexagon), name="add"
    )

    mod = hexagon_session.load_module(func)

    A_data = tvm.nd.array(np.array([2, 3], dtype=dtype), device=hexagon_session.device)
    assert (A_data.numpy() == np.array([2, 3])).all()
    B_data = tvm.nd.array(np.array([4], dtype=dtype), device=hexagon_session.device)
    assert (B_data.numpy() == np.array([4])).all()
    C_data = tvm.nd.array(np.array([0, 0], dtype=dtype), device=hexagon_session.device)
    assert (C_data.numpy() == np.array([0, 0])).all()
    mod["add"](A_data, B_data, C_data)
    assert (C_data.numpy() == np.array([6, 7])).all()


@tvm.testing.requires_hexagon
def test_add_vtcm(hexagon_session: Session):
    dtype = "int8"
    A = tvm.te.placeholder((2,), dtype=dtype)
    B = tvm.te.placeholder((1,), dtype=dtype)
    C = tvm.te.compute(A.shape, lambda i: A[i] + B[0], name="C")
    sched = tvm.te.create_schedule(C.op)

    target_hexagon = tvm.target.hexagon("v68", link_params=True)
    func = tvm.build(
        sched, [A, B, C], tvm.target.Target(target_hexagon, host=target_hexagon), name="add"
    )

    mod = hexagon_session.load_module(func)

    A_data = tvm.nd.empty(A.shape, A.dtype, hexagon_session.device, "global.vtcm")
    A_data.copyfrom(np.array([2, 3]))

    B_data = tvm.nd.empty(B.shape, B.dtype, hexagon_session.device, "global.vtcm")
    B_data.copyfrom(np.array([4]))

    C_data = tvm.nd.empty(C.shape, C.dtype, hexagon_session.device, "global.vtcm")
    C_data.copyfrom(np.array([0, 0]))

    mod["add"](A_data, B_data, C_data)
    result = C_data.numpy()
    assert (result == np.array([6, 7])).all()


class TestMatMul:
    M = tvm.testing.parameter(32)
    N = tvm.testing.parameter(32)
    K = tvm.testing.parameter(32)

    @tvm.testing.requires_hexagon
    def test_matmul(self, hexagon_session, M, N, K):
        X = te.placeholder((M, K), dtype="float32")
        Y = te.placeholder((K, N), dtype="float32")
        k1 = te.reduce_axis((0, K), name="k1")
        Z = te.compute((M, N), lambda i, j: te.sum(X[i, k1] * Y[k1, j], axis=[k1]))
        schedule = te.create_schedule(Z.op)

        target_hexagon = tvm.target.hexagon("v68", link_params=True)
        func = tvm.build(
            schedule, [X, Y, Z], tvm.target.Target(target_hexagon, host=target_hexagon)
        )

        mod = hexagon_session.load_module(func)

        x = np.random.uniform(size=[i.value for i in X.shape]).astype(X.dtype)
        y = np.random.uniform(size=[i.value for i in Y.shape]).astype(Y.dtype)
        z = np.zeros([i.value for i in Z.shape], dtype=Z.dtype)

        xt = tvm.nd.array(x, device=hexagon_session.device)
        yt = tvm.nd.array(y, device=hexagon_session.device)
        zt = tvm.nd.array(z, device=hexagon_session.device)
        mod(xt, yt, zt)

        target_llvm = tvm.target.Target("llvm")
        mod = tvm.build(schedule, [X, Y, Z], tvm.target.Target(target_llvm, host=target_llvm))
        device = tvm.cpu(0)
        xtcpu = tvm.nd.array(x, device)
        ytcpu = tvm.nd.array(y, device)
        ztcpu = tvm.nd.array(z, device)
        mod(xtcpu, ytcpu, ztcpu)

        tvm.testing.assert_allclose(zt.numpy(), ztcpu.numpy(), rtol=1e-4)


@tvm.testing.requires_hexagon
def test_graph_executor(hexagon_session: Session):
    dtype = "float32"
    data = relay.var("data", relay.TensorType((1, 64, 64, 3), dtype))
    weight = relay.var("weight", relay.TensorType((5, 5, 3, 8), dtype))
    y = relay.nn.conv2d(
        data,
        weight,
        padding=(2, 2),
        kernel_size=(5, 5),
        data_layout="NHWC",
        kernel_layout="HWIO",
        out_dtype="float32",
    )
    f = relay.Function([data, weight], y)
    relay_mod = tvm.IRModule.from_expr(f)
    relay_mod = relay.transform.InferType()(relay_mod)

    target_hexagon = tvm.target.hexagon("v68")
    runtime = Runtime("cpp")
    executor = Executor("graph")

    weight_in = np.random.rand(5, 5, 3, 8).astype(dtype=dtype)
    data_in = np.random.rand(1, 64, 64, 3).astype(dtype=dtype)
    params = {"weight": weight_in}
    inputs = {"data": data_in}

    with tvm.transform.PassContext(opt_level=3):
        lowered = tvm.relay.build(
            relay_mod,
            tvm.target.Target(target_hexagon, host=target_hexagon),
            runtime=runtime,
            executor=executor,
        )

    graph_mod = hexagon_session.get_executor_from_factory(lowered)
    graph_mod.set_input(**params)
    graph_mod.run(**inputs)
    hexagon_output = graph_mod.get_output(0).numpy()

    target_llvm = tvm.target.Target("llvm")
    with tvm.transform.PassContext(opt_level=3):
        llvm_lowered = tvm.relay.build(
            relay_mod,
            tvm.target.Target(target_llvm, host=target_llvm),
            runtime=runtime,
            executor=executor,
        )
    llvm_graph_mod = tvm.contrib.graph_executor.GraphModule(llvm_lowered["default"](tvm.cpu(0)))
    llvm_graph_mod.set_input(**params)
    llvm_graph_mod.run(**inputs)
    expected_output = llvm_graph_mod.get_output(0).numpy()

    tvm.testing.assert_allclose(hexagon_output, expected_output, rtol=1e-4, atol=1e-5)


@tvm.testing.requires_hexagon
def test_graph_executor_multiple_conv2d(hexagon_session: Session):
    dtype = "float32"
    input_shape = (1, 8, 8, 3)
    w1_shape = (5, 5, 3, 1)
    w2_shape = (5, 5, 1, 3)
    data = relay.var("data", relay.TensorType(input_shape, dtype))
    weight1 = relay.var("weight1", relay.TensorType(w1_shape, dtype))
    weight2 = relay.var("weight2", relay.TensorType(w2_shape, dtype))
    y1 = relay.nn.conv2d(
        data,
        weight1,
        padding=(2, 2),
        kernel_size=(5, 5),
        data_layout="NHWC",
        kernel_layout="HWIO",
        out_dtype="float32",
    )
    y2 = relay.nn.conv2d(
        y1,
        weight2,
        padding=(2, 2),
        kernel_size=(5, 5),
        data_layout="NHWC",
        kernel_layout="HWIO",
        out_dtype="float32",
    )
    f = relay.Function([data, weight1, weight2], y2)
    relay_mod = tvm.IRModule.from_expr(f)
    relay_mod = relay.transform.InferType()(relay_mod)

    target_hexagon = tvm.target.hexagon("v68")
    runtime = Runtime("cpp")
    executor = Executor("graph")

    with tvm.transform.PassContext(opt_level=3):
        lowered = tvm.relay.build(
            relay_mod,
            tvm.target.Target(target_hexagon, host=target_hexagon),
            runtime=runtime,
            executor=executor,
        )

    weight1_data = np.random.rand(w1_shape[0], w1_shape[1], w1_shape[2], w1_shape[3]).astype(
        dtype=dtype
    )
    weight2_data = np.random.rand(w2_shape[0], w2_shape[1], w2_shape[2], w2_shape[3]).astype(
        dtype=dtype
    )
    input_data = np.random.rand(
        input_shape[0], input_shape[1], input_shape[2], input_shape[3]
    ).astype(dtype=dtype)

    params = {"weight1": weight1_data, "weight2": weight2_data}
    inputs = {"data": input_data}

    graph_mod = hexagon_session.get_executor_from_factory(lowered)
    graph_mod.set_input(**params)
    graph_mod.run(**inputs)
    hexagon_output = graph_mod.get_output(0).numpy()

    target_llvm = tvm.target.Target("llvm")
    with tvm.transform.PassContext(opt_level=3):
        llvm_lowered = tvm.relay.build(
            relay_mod,
            tvm.target.Target(target_llvm, host=target_llvm),
            runtime=runtime,
            executor=executor,
        )
    llvm_graph_mod = tvm.contrib.graph_executor.GraphModule(llvm_lowered["default"](tvm.cpu(0)))
    llvm_graph_mod.set_input(**params)
    llvm_graph_mod.run(**inputs)
    expected_output = llvm_graph_mod.get_output(0).numpy()

    tvm.testing.assert_allclose(hexagon_output, expected_output, rtol=1e-4, atol=1e-5)


@tvm.testing.requires_hexagon
def test_aot_executor(hexagon_session: Session, aot_host_target, aot_target):
    dtype = "float32"
    input_shape = (1, 128, 128, 3)
    w_shape = (5, 5, 3, 8)
    data = relay.var("data", relay.TensorType(input_shape, dtype))
    weight = relay.var("weight", relay.TensorType(w_shape, dtype))
    y = relay.nn.conv2d(
        data,
        weight,
        padding=(2, 2),
        kernel_size=(5, 5),
        data_layout="NHWC",
        kernel_layout="HWIO",
        out_dtype="float32",
    )
    f = relay.Function([data, weight], y)
    relay_mod = tvm.IRModule.from_expr(f)
    relay_mod = relay.transform.InferType()(relay_mod)

    weight_data = np.random.rand(w_shape[0], w_shape[1], w_shape[2], w_shape[3]).astype(dtype=dtype)
    input_data = np.random.rand(
        input_shape[0], input_shape[1], input_shape[2], input_shape[3]
    ).astype(dtype=dtype)

    params = {"weight": weight_data}
    inputs = {"data": input_data}

    with tvm.transform.PassContext(opt_level=3):
        lowered = tvm.relay.build(
            relay_mod,
            params=params,
            target=tvm.target.Target(aot_target, host=aot_host_target),
            runtime=Runtime("cpp"),
            executor=Executor("aot", {"unpacked-api": False, "interface-api": "packed"}),
        )

    aot_mod = hexagon_session.get_executor_from_factory(lowered)
    aot_mod.set_input(**inputs)
    aot_mod.run()
    hexagon_output = aot_mod.get_output(0).numpy()

    target_llvm = tvm.target.Target("llvm")
    with tvm.transform.PassContext(opt_level=3):
        llvm_lowered = tvm.relay.build(
            relay_mod,
            tvm.target.Target(target_llvm, host=target_llvm),
            runtime=Runtime("cpp"),
            executor=Executor("graph"),
        )

    llvm_graph_mod = tvm.contrib.graph_executor.GraphModule(llvm_lowered["default"](tvm.cpu(0)))
    llvm_graph_mod.set_input(**params)
    llvm_graph_mod.run(**inputs)
    expected_output = llvm_graph_mod.get_output(0).numpy()

    tvm.testing.assert_allclose(hexagon_output, expected_output, rtol=1e-4, atol=1e-5)


@tvm.testing.requires_hexagon
def test_aot_executor_multiple_conv2d(hexagon_session: Session, aot_host_target, aot_target):
    dtype = "float32"
    input_shape = (1, 8, 8, 3)
    w1_shape = (5, 5, 3, 1)
    w2_shape = (5, 5, 1, 3)
    data = relay.var("data", relay.TensorType(input_shape, dtype))
    weight1 = relay.var("weight1", relay.TensorType(w1_shape, dtype))
    weight2 = relay.var("weight2", relay.TensorType(w2_shape, dtype))
    y1 = relay.nn.conv2d(
        data,
        weight1,
        padding=(2, 2),
        kernel_size=(5, 5),
        data_layout="NHWC",
        kernel_layout="HWIO",
        out_dtype="float32",
    )
    y2 = relay.nn.conv2d(
        y1,
        weight2,
        padding=(2, 2),
        kernel_size=(5, 5),
        data_layout="NHWC",
        kernel_layout="HWIO",
        out_dtype="float32",
    )
    f = relay.Function([data, weight1, weight2], y2)
    relay_mod = tvm.IRModule.from_expr(f)
    relay_mod = relay.transform.InferType()(relay_mod)

    weight1_data = np.random.rand(w1_shape[0], w1_shape[1], w1_shape[2], w1_shape[3]).astype(
        dtype=dtype
    )
    weight2_data = np.random.rand(w2_shape[0], w2_shape[1], w2_shape[2], w2_shape[3]).astype(
        dtype=dtype
    )
    input_data = np.random.rand(
        input_shape[0], input_shape[1], input_shape[2], input_shape[3]
    ).astype(dtype=dtype)

    params = {"weight1": weight1_data, "weight2": weight2_data}
    inputs = {"data": input_data}

    with tvm.transform.PassContext(opt_level=3):
        lowered = tvm.relay.build(
            relay_mod,
            params=params,
            target=tvm.target.Target(aot_target, host=aot_host_target),
            runtime=Runtime("cpp"),
            executor=Executor("aot", {"unpacked-api": False, "interface-api": "packed"}),
        )

    aot_mod = hexagon_session.get_executor_from_factory(lowered)
    aot_mod.set_input(**inputs)
    aot_mod.run()
    hexagon_output = aot_mod.get_output(0).numpy()

    target_llvm = tvm.target.Target("llvm")
    with tvm.transform.PassContext(opt_level=3):
        llvm_lowered = tvm.relay.build(
            relay_mod,
            tvm.target.Target(target_llvm, host=target_llvm),
            runtime=Runtime("cpp"),
            executor=Executor("graph"),
        )

    llvm_graph_mod = tvm.contrib.graph_executor.GraphModule(llvm_lowered["default"](tvm.cpu(0)))
    llvm_graph_mod.set_input(**params)
    llvm_graph_mod.run(**inputs)
    expected_output = llvm_graph_mod.get_output(0).numpy()

    tvm.testing.assert_allclose(hexagon_output, expected_output, rtol=1e-4, atol=1e-5)


def test_vm(hexagon_session: Session):
    dtype = "float32"
    input_shape = (1, 128, 128, 3)
    w_shape = (5, 5, 3, 8)
    data = relay.var("data", relay.TensorType(input_shape, dtype))
    weight = relay.var("weight", relay.TensorType(w_shape, dtype))
    y = relay.nn.conv2d(
        data,
        weight,
        padding=(2, 2),
        kernel_size=(5, 5),
        data_layout="NHWC",
        kernel_layout="HWIO",
        out_dtype="float32",
    )
    f = relay.Function([data, weight], y)
    relay_mod = tvm.IRModule.from_expr(f)
    relay_mod = relay.transform.InferType()(relay_mod)

    target_hexagon = tvm.target.hexagon("v68")

    weight_data = np.random.rand(w_shape[0], w_shape[1], w_shape[2], w_shape[3]).astype(dtype=dtype)
    input_data = np.random.rand(
        input_shape[0], input_shape[1], input_shape[2], input_shape[3]
    ).astype(dtype=dtype)

    params = {"weight": weight_data}
    inputs = {"data": input_data}

    from tvm.relay.backend import vm

    with tvm.transform.PassContext(opt_level=3):
        exe = vm.compile(
            relay_mod,
            # tvm.target.Target(target_hexagon, host=target_hexagon),
            target=target_hexagon,
            target_host=target_hexagon,
            params=params,
        )
    relay_vm = hexagon_session.get_executor_from_factory(exe)

    relay_vm.set_input(func_name="main", **inputs)

    relay_vm.run()

    output = relay_vm.get_outputs()
    output = output[0].numpy()

    return output


if __name__ == "__main__":
    # test_relax_dyn_shape()
    tvm.testing.main()
