"""ReAct-like 工作流的最大步数终态收口。

本模块承载「步数耗尽统一落定失败」的逻辑，不是 LangGraph graph 节点：它由 ``model_node``
作为普通 async 函数直接调用（不再经条件边路由），输出终态 state patch_write。保持独立文件
（而非并入 model_node）是因为该逻辑横跨 canonical writer / run 标记 / 终态 patch_write 构造，
职责清晰。
"""

from typing import Any

from app.config.logging.logger import log
from app.core.workflows.nodes.helper.common import (
    _runtime_config,
    terminal_state,
)
from app.core.workflows.react.state import ReactGraphState

# 步数耗尽时写给父 Agent / 用户的默认可见文本（英文，与面向模型的文本约定一致）。
# 正常完成路径的 final_text 是模型最终回答；步数耗尽没有最终回答，故给一个明确
# 的失败说明，让父 Agent / 用户能感知「因步数耗尽而停止」，而不是静默失败。
_MAX_STEPS_FINAL_TEXT = (
    "The agent stopped after reaching the maximum number of steps "
    "before producing a final answer."
)


async def _finalize_max_steps(
    state: ReactGraphState, *, step_count: int | None = None
) -> dict[str, Any]:
    """统一落定 ``max_steps_reached`` 失败，返回终态 state patch_write。

    这是被 ``model_node`` 直接调用的收口函数（非 LangGraph graph 节点）。``model_node``
    在发起推理前发现本次推理超配额（``step_count > max_steps``）时调用本函数，保证超配额
    后不再发起推理、终态分类恒为 ``max_steps_reached``。``step_count`` 缺省时回退读
    ``state.step_count``（主要供测试直接调用；生产路径 ``model_node`` 恒显式传入）。

    终态 patch_write 会把 ``_MAX_STEPS_FINAL_TEXT`` 写入 ``final_text``（供 checkpoint 留存），
    且 canonical run 终态经 ``end_reason="max_steps_reached"`` 保存，使前端与父 Agent
    能按稳定原因分类渲染「因步数耗尽而停止」的可读说明。

    参数:
        state: 当前 graph state。
        step_count: 触发超配额的那一步序号（``model_node`` 传入 ``state.step_count + 1``）；
            缺省时用 ``state.step_count``。
    返回:
        写回 LangGraph state 的终态字段。
    异常:
        无。
    副作用:
        可能把当前 run 标记为 failed，并写入 canonical conversation state。
    """

    rc = _runtime_config()
    effective_step_count = state.step_count if step_count is None else step_count
    step_id = f"step-{effective_step_count}"
    run_id = getattr(rc.run, "id", None) or getattr(rc.run, "run_id", None)
    if run_id is None:
        raise RuntimeError("runtime config does not contain a run id")
    failed_run = rc.operations.fail_run_if_running(
        end_reason="max_steps_reached", usage_stats=rc.usage_stats,
    )
    if failed_run is None:
        log.info(
            "max_steps_node_terminal_race_lost",
            extra={
                "msg": f"最大步数失败落定时 run 已非 running，跳过终态写入，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": run_id},
            },
        )
        return _terminal_state(effective_step_count)

    event_data = {"final_text": _MAX_STEPS_FINAL_TEXT}
    log.warning(
        "max_steps_node_failed",
        extra={
            "msg": f"已达到最大步数上限，停止继续执行，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "step_count": effective_step_count,
                "max_steps": state.max_steps,
                **event_data,
            },
        },
    )
    # 失败原因与终态由 RuntimeOperations 的 canonical writer 原子落定；附加诊断
    # 数据只进入结构化日志，不重新引入通用 runtime event payload。
    return _terminal_state(effective_step_count)


def _terminal_state(step_count: int) -> dict[str, Any]:
    """构造最大步数收口函数返回的终态 state patch_write。

    复用 ``common.terminal_state`` 的终态字段（统一收口终态硬字段，与 observe 节点各分支口径一致），
    并补回本节点特有的 ``continuation_error_data=None``
    （终态不再有后续 continuation）与默认 ``final_text``（步数耗尽没有最终回答，
    写给父 Agent / 用户的可见失败说明，见 ``_MAX_STEPS_FINAL_TEXT``）。

    参数:
        step_count: 当前模型步数。
    返回:
        写回 LangGraph state 的终态字段。
    异常:
        无。
    副作用:
        无。
    """

    return {
        **terminal_state(step_count),
        "continuation_error_data": None,
        "final_text": _MAX_STEPS_FINAL_TEXT,
    }
