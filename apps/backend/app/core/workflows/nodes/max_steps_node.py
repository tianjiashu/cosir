"""ReAct-like 工作流的最大步数终态节点。"""

from app.config.logging.logger import log
from app.core.workflows.nodes.helper.common import (
    _runtime_config,
    build_run_failed_payload,
    terminal_state,
    write_event,
)
from app.models.enums.event_type import EventType

from ..react.state import ReactGraphState


async def _max_steps_node(state: ReactGraphState) -> dict[str, object]:
    """统一落定 ``max_steps_reached`` 失败。

    参数:
        state: 当前 graph state，通常来自条件边对继续动作的统一拦截。
    返回:
        写回 LangGraph state 的终态字段。
    异常:
        无。
    副作用:
        可能把当前 turn 标记为 failed，并写出 ``RUN_FAILED`` runtime event。
    """

    rc = _runtime_config()
    step_id = f"step-{state.step_count}"
    failed_turn = rc.operations.fail_turn_if_running(
        rc.turn.turn_id,
        end_reason="max_steps_reached",
    )
    if failed_turn is None:
        log.info(
            "max_steps_node_terminal_race_lost",
            extra={
                "msg": f"最大步数失败落定时 turn 已非 running，跳过失败事件，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": rc.turn.turn_id},
            },
        )
        return _terminal_state(state.step_count)

    event_data = dict(state.continuation_error_data or {})
    log.warning(
        "max_steps_node_failed",
        extra={
            "msg": f"已达到最大步数上限，停止继续执行，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "step_count": state.step_count,
                "max_steps": state.max_steps,
                **event_data,
            },
        },
    )
    write_event(
        EventType.RUN_FAILED,
        build_run_failed_payload(
            step_id,
            "max_steps_reached",
            usage=rc.usage_stats,
            langfuse_trace_id=rc.langfuse_trace_id,
            data=event_data or None,
        ),
    )
    return _terminal_state(state.step_count)


def _terminal_state(step_count: int) -> dict[str, object]:
    """构造最大步数节点返回的终态 state patch。

    复用 ``common.terminal_state`` 的终态字段，并补回本节点特有的
    ``continuation_error_data=None``（终态不再有后续 continuation）。

    参数:
        step_count: 当前模型步数。
    返回:
        写回 LangGraph state 的终态字段。
    异常:
        无。
    副作用:
        无。
    """

    return {**terminal_state(step_count), "continuation_error_data": None}
