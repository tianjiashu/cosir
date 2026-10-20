"""等待用户决定的持久化边界；只冻结目标调用，不保存工具生命周期副本。"""

from __future__ import annotations

from typing import Any, cast

from app.config.logging.logger import log
from app.core.tools.schemas import EXECUTING_DECISION_KINDS, UserDecision, UserDecisionKind
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.node_helper.common import _runtime_context
from app.core.workflow.node_helper.tool_observation_projection import project_waiting_request
from app.core.workflow.node_helper.user_input_projection import (
    apply_decision_to_observations,
    extract_request,
    request_call_id,
)
from app.models.conversation_run_progress import (
    ConversationRunProgress,
    RunWaitingState,
    ToolBatchSnapshot,
)


def _observations(state: ReactExecutionState) -> list[dict[str, Any]]:
    """读取本批观察；形状由等待快照与请求解析边界保证。"""
    return cast(list[dict[str, Any]], state.last_tool_results["observations"])


def wait_user_node(state: ReactExecutionState, decision: UserDecision) -> dict[str, object]:
    """消费已提交的决定；批准只重执行请求所属调用，拒绝直接结算观察。"""
    runtime = _runtime_context()
    observations = _observations(state)
    request = extract_request(observations)
    if request is None:
        raise ValueError("当前 Run 没有待决请求")
    forced = decision.kind is UserDecisionKind.FORCE_REJECT
    if forced:
        decision = UserDecision(
            request_id=request.request_id, kind=UserDecisionKind.REJECT, data=decision.data
        )
    if decision.request_id != request.request_id:
        raise ValueError(f"决定指向未知的等待请求：{decision.request_id}")
    if not forced and not request.accepts(decision.kind):
        raise ValueError(f"等待请求 {request.request_id} 不接受决定 {decision.kind.value}")
    if decision.kind is UserDecisionKind.REJECT:
        runtime.user_input_decision_handlers.handle_rejection(request, decision)

    amended = apply_decision_to_observations(observations, decision)
    patch: dict[str, object] = {
        "last_tool_results": {**state.last_tool_results, "observations": amended}
    }
    if decision.kind in EXECUTING_DECISION_KINDS:
        call_id = request_call_id(observations, request.request_id)
        pending = next((call for call in state.calls if call["id"] == call_id), None)
        if pending is None:
            raise KeyError(call_id)
        if pending["function"]["name"] not in runtime.operations.allows_tools:
            raise ValueError(f"被本轮工具白名单拦下的调用不能被批准执行：{call_id}")
        next_node = ReactRoute.TOOLS
        patch.update(calls=[pending], replay_decision=decision, next_node=next_node)
    else:
        next_node = ReactRoute.OBSERVE
        patch["next_node"] = next_node
    log.info(
        "wait_user_node_resolved",
        extra={
            "msg": "用户决定已消费，工作流继续",
            "data": {
                "run_id": runtime.run.id,
                "request_id": request.request_id,
                "decision": decision.kind.value,
                "next_node": next_node.value,
            },
        },
    )
    return patch


def suspend_for_input(state: ReactExecutionState) -> None:
    """先投影并持久化待决卡片，再原子保存恢复所需的单条调用与观察。"""
    runtime = _runtime_context()
    observations = _observations(state)
    request = extract_request(observations)
    if request is None:
        raise ValueError("等待用户时缺少待决请求")
    call_id = request_call_id(observations, request.request_id)
    pending = next((call for call in state.calls if call["id"] == call_id), None)
    if pending is None:
        raise KeyError(call_id)
    summary = next(item for item in observations if item["tool_call_id"] == call_id)
    project_waiting_request(summary, step_id=f"step-{state.step_count}")
    runtime.operations.mark_waiting_for_input_if_running(
        runtime.usage_stats,
        execution_data=ConversationRunProgress(
            step_count=state.step_count,
            tool_error_count=state.tool_error_count,
            waiting=RunWaitingState(
                instruction=state.instruction,
                last_tool_results=ToolBatchSnapshot.model_validate(state.last_tool_results),
                pending_call=pending,
                projected_tool_calls=dict(runtime.step_context.projected_tool_calls),
            ),
        ),
    )
