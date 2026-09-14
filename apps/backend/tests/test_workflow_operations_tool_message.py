from langchain_core.messages import ToolMessage

from app.core.tools.schemas import ToolObservation
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.workflows.workflow_operations import WorkflowOperations


def _to_message(observation: ToolObservation) -> ToolMessage:
    return WorkflowOperations._to_model_message(object(), observation)


def test_success_message_only_contains_output_or_success_fallback() -> None:
    message = _to_message(
        ToolObservation(
            tool_name="read_file",
            status="success",
            content="file content",
            tool_call_id="call-1",
        )
    )

    assert message.content == "file content"
    assert message.status == "success"
    assert message.tool_call_id == "call-1"

    empty_message = _to_message(
        ToolObservation(tool_name="write_file", status="success", content="")
    )
    assert empty_message.content == "success"


def test_error_message_contains_only_diagnostic_retry_signal_and_reason() -> None:
    message = _to_message(
        ToolObservation(
            tool_name="web_search",
            status="error",
            content="same error",
            error="provider unavailable",
            reason="retry after a short delay",
            retryable=True,
            tool_call_id="call-2",
        )
    )

    assert message.content == (
        "error: provider unavailable\n"
        "retryable: true\n"
        "hint: this error can be retried; decide from the context whether "
        "retrying is appropriate.\n"
        "reason: retry after a short delay"
    )
    assert message.status == "error"


def test_non_retryable_error_does_not_emit_optional_retry_hint() -> None:
    message = _to_message(
        ToolObservation(
            tool_name="apply_patch",
            status="error",
            error="invalid patch_write",
            reason="fix the patch_write format before calling again",
            retryable=False,
        )
    )

    assert message.content == (
        "error: invalid patch_write\n"
        "retryable: false\n"
        "hint: do not retry this tool call.\n"
        "reason: fix the patch_write format before calling again"
    )


def test_cancelled_message_is_distinct_and_uses_langchain_compatible_status() -> None:
    observation = tool_cancelled(
        "execute_terminal",
        tool_call_id="call-3",
    )
    message = _to_message(observation)

    assert observation.content is None
    assert observation.error is None
    assert observation.reason == (
        "the tool call was cancelled before completion; no result was produced."
    )
    assert message.content == (
        "cancelled: the tool call was cancelled before completion; no result was produced."
    )
    assert message.status == "error"
    assert "retryable" not in message.content
