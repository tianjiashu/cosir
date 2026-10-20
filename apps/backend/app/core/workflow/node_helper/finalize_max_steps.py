"""ReAct-like 工作流的最大步数终态收口。

本模块承载「步数耗尽统一落定失败」的逻辑，不是 loop 步骤：它由 ``model_node``
作为普通 async 函数直接调用（不再经条件边路由），输出终态 state patch_write。保持独立文件
（而非并入 model_node）是因为该逻辑横跨 canonical writer / run 标记 / 终态 patch_write 构造，
职责清晰。
"""

from typing import Any

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.node_helper.common import (
    _runtime_context,
    route_state,
)

# 步数耗尽时写给父 Agent / 用户的默认可见文本（英文，与面向模型的文本约定一致）。
# 正常完成路径的最终回答是模型回答；步数耗尽没有最终回答，故用这段失败说明作为 Run 终态
# 输出，让父 Agent / 用户能感知「因步数耗尽而停止」，而不是静默失败。


async def _finalize_max_steps(
    state: ReactExecutionState, *, step_count: int | None = None
) -> dict[str, Any]:
    """统一落定 ``max_steps_reached`` 失败，返回终态 state patch_write。

    这是被 ``model_node`` 直接调用的收口函数（非 loop 步骤）。``model_node``
    在发起推理前发现本次推理超配额（``step_count > max_steps``）时调用本函数，保证超配额
    后不再发起推理、终态分类恒为 ``max_steps_reached``。``step_count`` 缺省时回退读
    ``state.step_count``（主要供测试直接调用；生产路径 ``model_node`` 恒显式传入）。

    终态由 canonical run 承载：``end_reason="max_steps_reached"`` 与
    ``MAX_STEPS_FINAL_TEXT`` 作为 ``final_output`` 一次写入，前端与父 Agent 据此按稳定原因
    分类渲染「因步数耗尽而停止」；执行态只回传结束路由，不再另带一份最终文本。

    参数:
        state: 当前 graph state。
        step_count: 触发超配额的那一步序号（``model_node`` 传入 ``state.step_count + 1``）；
            缺省时用 ``state.step_count``。

    返回:
        写回执行 state 的终态字段（仅结束路由）。

    异常:
        RuntimeError: ``RuntimeContext`` 未携带可用 run id（缺少 ``id``）。

    副作用:
        可能把当前 run 标记为 failed，并写入 canonical conversation state。
    """

    rc = _runtime_context()
    effective_step_count = state.step_count if step_count is None else step_count
    step_id = f"step-{effective_step_count}"
    run_id = getattr(rc.run, "id", None) or getattr(rc.run, "run_id", None)
    if run_id is None:
        raise RuntimeError("runtime config does not contain a run id")
    failed_run = rc.operations.fail_run_if_running(
        end_reason="max_steps_reached",
        usage_stats=rc.usage_stats,
        final_output=Constant.Workflow.MAX_STEPS_FINAL_TEXT,
    )
    if failed_run is None:
        log.info(
            "max_steps_node_terminal_race_lost",
            extra={
                "msg": f"最大步数失败落定时 run 已非 running，跳过终态写入，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": run_id},
            },
        )
        return _end_state(effective_step_count)

    event_data = {"final_output": Constant.Workflow.MAX_STEPS_FINAL_TEXT}
    log.warning(
        "max_steps_node_failed",
        extra={
            "msg": f"已达到最大步数上限，停止继续执行，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "step_count": effective_step_count,
                "max_steps": rc.max_steps,
                **event_data,
            },
        },
    )
    # 失败原因与终态由 WorkflowOperations 经 canonical writer 原子落定；附加诊断
    # 数据只进入结构化日志，不进入事件 payload。
    return _end_state(effective_step_count)


def _end_state(step_count: int) -> dict[str, Any]:
    """构造最大步数收口函数返回的结束路由 state 增量。

    参数:
        step_count: 当前模型步数。

    返回:
        写回执行 state 的终态字段（仅结束路由）。

    异常:
        无。

    副作用:
        无。
    """

    return route_state(step_count, ReactRoute.END)
