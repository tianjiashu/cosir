from openai.types.chat import ChatCompletionToolMessageParam

from app.core.tools.schemas import ToolObservation
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.workflow.workflow_operations import WorkflowOperations


def _to_message(observation: ToolObservation) -> ChatCompletionToolMessageParam:
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

    assert message.get("content") == "file content"
    assert "status" not in message
    assert message.get("tool_call_id") == "call-1"

    empty_message = _to_message(
        ToolObservation(tool_name="write_file", status="success", content="")
    )
    assert empty_message.get("content") == "success"


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

    # ``retryable`` 不再单独成行：重试信号由 hint 承载，避免同一信息重复发送给模型。
    assert message.get("content") == (
        "error: provider unavailable\n"
        "hint: this error can be retried; decide from the context whether "
        "retrying is appropriate.\n"
        "reason: retry after a short delay"
    )
    assert "status" not in message


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

    assert message.get("content") == (
        "error: invalid patch_write\n"
        "hint: do not retry this tool call.\n"
        "reason: fix the patch_write format before calling again"
    )


def test_cancelled_message_is_distinct_without_business_status_in_protocol() -> None:
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
    assert message.get("content") == (
        "cancelled: the tool call was cancelled before completion; no result was produced."
    )
    assert "status" not in message
    assert "retryable" not in message.get("content")
