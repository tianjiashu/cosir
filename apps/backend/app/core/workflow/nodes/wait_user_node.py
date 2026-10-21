"""等待用户决定的持久化边界；只冻结目标调用，不保存工具生命周期副本。"""

from __future__ import annotations

from app.config.configuration import get_tool_system
from app.config.logging.logger import log
from app.core.tools.schemas import EXECUTING_DECISION_KINDS, UserDecision, UserDecisionKind
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.node_helper.common import RunStopped, _runtime_context
from app.core.workflow.node_helper.tool_observation_projection import project_waiting_request
from app.core.workflow.node_helper.user_input_projection import (
    apply_decision_to_observations,
    extract_request,
    request_call_id,
)
from app.core.workflow.tool_continuation import freeze_tool_continuation
from app.models.conversation_run_progress import ConversationRunProgress
from app.models.enums.conversation_run_status import RunTransition
from app.service import depends as service_depends


async def wait_user_node(state: ReactExecutionState) -> dict[str, object]:
    """等待用户决定的唯一节点：有决定就消费，否则挂起结束本次驱动。

    决定来源：等待恢复入口装入的 ``resume_decision``（已由 ``bind_decision`` 校验）；没有决定
    且 Task 收到取消信号时直接驳回当前请求，经 observe 回到 model，由 model 落定 cancelled。
    挂起以 ``RunStopped`` 结束驱动，避免循环在挂起后再保存一次进度、覆盖刚冻结的待决请求。
    """
    runtime = _runtime_context()
    decision = runtime.resume_decision
    runtime.resume_decision = None
    if decision is None:
        if not runtime.is_cancelled():
            suspend_for_input(state)
            raise RunStopped()
        request = extract_request(runtime.batch.observations)
        decision = UserDecision(request_id=request.request_id, kind=UserDecisionKind.REJECT)
    return apply_user_decision(decision)


def apply_user_decision(decision: UserDecision) -> dict[str, object]:
    """消费已校验的决定；批准只重执行请求所属调用，拒绝直接结算观察。

    决定在写进 Run 进度前已由 ``bind_decision`` 核对，这里不再重复校验请求与种类；本轮工具
    白名单只有执行期才知道，仍在这里拦截。
    """
    runtime = _runtime_context()
    batch = runtime.batch
    observations = batch.observations
    request = extract_request(observations)
    if decision.kind is UserDecisionKind.REJECT:
        get_tool_system().user_input_decision_handlers.handle_rejection(request, decision)

    amended = apply_decision_to_observations(observations, decision)
    batch.observations = amended
    if decision.kind in EXECUTING_DECISION_KINDS:
        call_id = request_call_id(observations, request.request_id)
        pending = next((call for call in batch.calls if call["id"] == call_id), None)
        if pending is None:
            raise KeyError(call_id)
        if pending["function"]["name"] not in runtime.allows_tools:
            raise ValueError(f"被本轮工具白名单拦下的调用不能被批准执行：{call_id}")
        next_node = ReactRoute.TOOLS
        batch.calls = [pending]
        batch.approved_call = (call_id, decision)
    else:
        next_node = ReactRoute.OBSERVE
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
    return {"next_node": next_node}


def suspend_for_input(state: ReactExecutionState) -> None:
    """先投影并持久化待决卡片，再原子保存恢复所需的单条调用与观察。"""
    runtime = _runtime_context()
    batch = runtime.batch
    observations = batch.observations
    request = extract_request(observations)
    if request is None:
        raise ValueError("等待用户时缺少待决请求")
    call_id = request_call_id(observations, request.request_id)
    pending = next((call for call in batch.calls if call["id"] == call_id), None)
    if pending is None:
        raise KeyError(call_id)
    summary = next(item for item in observations if item["tool_call_id"] == call_id)
    project_waiting_request(summary, step_id=f"step-{state.step_count}")
    service_depends.get_conversation_run_state_service().transition(
        runtime.run.id,
        RunTransition.SUSPEND,
        usage_stats=runtime.usage_stats,
        execution_data=ConversationRunProgress(
            step_count=state.step_count,
            tool_error_count=state.tool_error_count,
            continuation=freeze_tool_continuation(batch, pending),
        ),
    )
