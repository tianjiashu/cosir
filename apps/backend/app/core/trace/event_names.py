"""Trace ledger 事件名治理。"""

from enum import StrEnum


class TraceEventName(StrEnum):
    """Trace ledger 使用的 canonical 事件名。"""

    RUN_CREATED = "run_created"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    WORKFLOW_STEP_STARTED = "workflow_step_started"
    MODEL_REQUESTED = "model_requested"
    MODEL_DELTA = "model_delta"
    MODEL_COMPLETED = "model_completed"
    TOOL_CALL_CREATED = "tool_call_created"
    TOOL_POLICY_DECIDED = "tool_policy_decided"
    TOOL_EXECUTION_PLANNED = "tool_execution_planned"
    TOOL_EXECUTION_STARTED = "tool_execution_started"
    TOOL_EXECUTION_COMPLETED = "tool_execution_completed"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    TOOL_EXECUTION_CANCELLED = "tool_execution_cancelled"
    TOOL_EXECUTION_TIMED_OUT = "tool_execution_timed_out"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    CHECKPOINT_CREATED = "checkpoint_created"
    CHECKPOINT_FAILED = "checkpoint_failed"
    RESUME_STARTED = "resume_started"
    RESUME_COMPLETED = "resume_completed"
    RESUME_FAILED = "resume_failed"
    RECOVERY_RECONCILED = "recovery_reconciled"


TRACE_EVENT_NAME_ALIASES = {
    "run_finished": TraceEventName.RUN_COMPLETED.value,
    "step_started": TraceEventName.WORKFLOW_STEP_STARTED.value,
    "model_output_delta": TraceEventName.MODEL_DELTA.value,
    "tool_execution_finished": TraceEventName.TOOL_EXECUTION_COMPLETED.value,
    "tool_call_finished": TraceEventName.TOOL_EXECUTION_COMPLETED.value,
    "tool_call_started": TraceEventName.TOOL_EXECUTION_STARTED.value,
    "tool_call_requested": TraceEventName.TOOL_CALL_CREATED.value,
    "tool_approval_required": TraceEventName.APPROVAL_REQUESTED.value,
    "checkpoint_created": TraceEventName.CHECKPOINT_CREATED.value,
    "checkpoint_failed": TraceEventName.CHECKPOINT_FAILED.value,
}


def canonical_event_name(event_name: str) -> str:
    """返回 trace ledger canonical 事件名。

    参数:
        event_name: Runtime、Tool Platform 或 Trace 层传入的事件名。

    返回:
        canonical trace ledger 事件名；未知事件保持原样返回，便于扩展事件落账。

    异常:
        无。

    副作用:
        无。
    """

    return TRACE_EVENT_NAME_ALIASES.get(event_name, event_name)


def runtime_trace_event_name(event_name: str, payload: dict) -> str:
    """返回运行时事件对应的 canonical trace event 名称。

    参数:
        event_name: RuntimeEvent、Tool Platform 或 Trace 层传入的事件名。
        payload: 运行时事件载荷，用于区分工具完成状态。

    返回:
        canonical trace event 名称；空字符串表示该事件由领域服务负责落账。

    异常:
        无。

    副作用:
        无。
    """

    if event_name == "tool_approval_required":
        return ""
    if event_name == "tool_call_finished":
        return _tool_finish_event_name(str(payload.get("status") or ""))
    return canonical_event_name(event_name)


def _tool_finish_event_name(status: str) -> str:
    """根据工具完成状态返回 canonical trace event 名称。

    参数:
        status: 工具观测状态。

    返回:
        tool_execution_* 事件名。

    异常:
        无。

    副作用:
        无。
    """

    if status in {"success", "completed", "succeeded"}:
        return TraceEventName.TOOL_EXECUTION_COMPLETED.value
    if status == "cancelled":
        return TraceEventName.TOOL_EXECUTION_CANCELLED.value
    if status in {"timed_out", "timeout"}:
        return TraceEventName.TOOL_EXECUTION_TIMED_OUT.value
    if status == "approval_required":
        return TraceEventName.TOOL_EXECUTION_PLANNED.value
    return TraceEventName.TOOL_EXECUTION_FAILED.value
