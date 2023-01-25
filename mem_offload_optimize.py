from typing import List, Dict
import torch
import torch.fx
from torch.fx import GraphModule

from torch.utils._pytree import tree_map

from colossalai.fx import ColoTracer, is_compatible_with_meta
from colossalai.fx.passes.meta_info_prop import MetaInfoProp

from strategies_constructor import OffloadStrategiesConstructor
from solver import AsynGreedySolver
from runtime import runtime_asyn_offload_apply_pass
from basic_offload_module import BasicOffloadModule
from util import compute_max_param_mem, compute_total_param_mem, compute_act_peak_mem


def memory_optimization(model: torch.nn.Module,
                        inps: Dict[str, torch.Tensor],
                        memory_budget: float=-1.0,
                        is_syn: bool=True):
    model.cpu()
    tracer = ColoTracer()
    assert is_compatible_with_meta()
    # wrap_fn = lambda x: MetaTensor(x, fake_device=torch.device("cpu")) if isinstance(x, torch.Tensor) else x
    wrap_fn = lambda x: x.to("meta") if isinstance(x, torch.Tensor) else x
    meta_args = tree_map(wrap_fn, inps)
    graph = tracer.trace(model, meta_args=meta_args)
    # graph.print_tabular()
    gm = GraphModule(model, graph, model.__class__.__name__)

    interp = MetaInfoProp(gm)
    interp.propagate(*meta_args.values())

    offload_strategies_constructor = OffloadStrategiesConstructor(graph)
    region_list = offload_strategies_constructor._linearize_graph()

    solver = AsynGreedySolver(region_list, memory_budget)
    solver._call_solver_greedy()

    # print offload node
    print("****************** offload plan *******************")
    for region in region_list:
        if region.is_offload or (region.region_to_prefetch is not None):
            print(region.r_id, region.region_to_prefetch, region.is_offload)

    act_peak_mem = compute_act_peak_mem(region_list)/1024**2
    max_param_mem = compute_max_param_mem(region_list)/1024**2
    total_param_mem = compute_total_param_mem(region_list)/1024**2
    print(f"act_peak_mem={act_peak_mem} MB | max_param_mem={max_param_mem} MB | total_param_mem={total_param_mem}")

    gm = runtime_asyn_offload_apply_pass(gm, region_list)

    gm.recompile()
    # print(gm.code)
    optimized_model = BasicOffloadModule(gm)
    return optimized_model