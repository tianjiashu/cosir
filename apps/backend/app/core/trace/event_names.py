"""Canonical trace event names."""

from enum import StrEnum


class TraceEventName(StrEnum):
    """Canonical event names used by the trace ledger."""

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


TRACE_EVENT_NAME_ALIASES = {
    "run_finished": TraceEventName.RUN_COMPLETED.value,
    "step_started": TraceEventName.WORKFLOW_STEP_STARTED.value,
    "model_output_delta": TraceEventName.MODEL_DELTA.value,
    "tool_execution_finished": TraceEventName.TOOL_EXECUTION_COMPLETED.value,
    "tool_call_finished": TraceEventName.TOOL_EXECUTION_COMPLETED.value,
    "tool_call_started": TraceEventName.TOOL_EXECUTION_STARTED.value,
    "tool_call_requested": TraceEventName.TOOL_CALL_CREATED.value,
}


def canonical_event_name(event_name: str) -> str:
    """Return the canonical trace event name.

    Parameters:
        event_name: Runtime, tool platform, or trace-layer event name.

    Returns:
        Canonical trace event name, or the original value if no alias exists.

    Raises:
        None.

    Side effects:
        None.
    """

    return TRACE_EVENT_NAME_ALIASES.get(event_name, event_name)


def runtime_trace_event_name(event_name: str, payload: dict) -> str:
    """Return the canonical trace event name for a runtime event.

    Parameters:
        event_name: Runtime, tool platform, or trace-layer event name.
        payload: Runtime event payload used to classify tool completion status.

    Returns:
        Canonical trace event name.

    Raises:
        None.

    Side effects:
        None.
    """

    if event_name == "tool_call_finished":
        return _tool_finish_event_name(str(payload.get("status") or ""))
    return canonical_event_name(event_name)


def _tool_finish_event_name(status: str) -> str:
    """Map a tool completion status to a canonical trace event name.

    Parameters:
        status: Tool observation status.

    Returns:
        ``tool_execution_*`` event name.

    Raises:
        None.

    Side effects:
        None.
    """

    if status in {"success", "completed", "succeeded"}:
        return TraceEventName.TOOL_EXECUTION_COMPLETED.value
    if status == "cancelled":
        return TraceEventName.TOOL_EXECUTION_CANCELLED.value
    if status in {"timed_out", "timeout"}:
        return TraceEventName.TOOL_EXECUTION_TIMED_OUT.value
    return TraceEventName.TOOL_EXECUTION_FAILED.value
