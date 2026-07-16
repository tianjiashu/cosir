"""Replay 节点类型与事件归类。"""

from enum import StrEnum


class ReplayNodeType(StrEnum):
    """Agent Replay 支持的稳定节点类型。"""

    USER_MESSAGE = "user_message"
    RUN_STARTED = "run_started"
    WORKFLOW_STEP = "workflow_step"
    MODEL_CALL = "model_call"
    MODEL_OUTPUT = "model_output"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL_WAIT = "approval_wait"
    APPROVAL_DECISION = "approval_decision"
    CHECKPOINT = "checkpoint"
    RESUME = "resume"
    ARTIFACT = "artifact"
    FINAL_RESPONSE = "final_response"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


TERMINAL_EVENT_TYPES = {
    "run_completed",
    "run_finished",
    "run_failed",
    "run_cancelled",
}


TOOL_EVENT_TYPES = {
    "tool_call_created",
    "tool_policy_decided",
    "tool_execution_planned",
    "tool_execution_started",
    "tool_execution_completed",
    "tool_execution_failed",
    "tool_execution_cancelled",
    "tool_execution_timed_out",
}


def replay_type_for_event(event_type: str) -> ReplayNodeType:
    """返回单个 trace event 的默认 replay 节点类型。

    参数:
        event_type: trace ledger event 类型。

    返回:
        对应 ReplayNodeType；未知事件归为 workflow_step。

    异常:
        无。

    副作用:
        无。
    """

    mapping = {
        "run_created": ReplayNodeType.RUN_STARTED,
        "run_started": ReplayNodeType.RUN_STARTED,
        "run_completed": ReplayNodeType.RUN_COMPLETED,
        "run_finished": ReplayNodeType.RUN_COMPLETED,
        "run_failed": ReplayNodeType.RUN_FAILED,
        "run_cancelled": ReplayNodeType.RUN_CANCELLED,
        "model_requested": ReplayNodeType.MODEL_CALL,
        "model_delta": ReplayNodeType.MODEL_OUTPUT,
        "model_completed": ReplayNodeType.MODEL_CALL,
        "model_failed": ReplayNodeType.MODEL_CALL,
        "approval_requested": ReplayNodeType.APPROVAL_WAIT,
        "approval_decided": ReplayNodeType.APPROVAL_DECISION,
        "resume_started": ReplayNodeType.RESUME,
        "resume_completed": ReplayNodeType.RESUME,
        "resume_failed": ReplayNodeType.RESUME,
        "recovery_reconciled": ReplayNodeType.RESUME,
        "checkpoint_created": ReplayNodeType.CHECKPOINT,
        "checkpoint_failed": ReplayNodeType.CHECKPOINT,
        "final_response": ReplayNodeType.FINAL_RESPONSE,
        "workflow_step_started": ReplayNodeType.WORKFLOW_STEP,
    }
    if event_type in TOOL_EVENT_TYPES:
        return ReplayNodeType.TOOL_CALL
    return mapping.get(event_type, ReplayNodeType.WORKFLOW_STEP)
