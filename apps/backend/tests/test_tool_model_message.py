"""工具观察转模型可见 tool 消息的最小契约。

失败 / 取消文本由工厂（``tool_error`` / ``tool_cancelled``）唯一拼装进 ``content``，
投影层只把 ``content`` 原样转交模型，空内容时回落为 ``status``。
"""

from app.core.tool.schemas import ToolObservation
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.workflow.node_helper.tool_observation_projection import (
    to_tool_model_message as _to_message,
)


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


def test_retryable_error_factory_builds_text_and_projection_passes_it_through() -> None:
    observation = tool_error(
        "web_search",
        "provider unavailable",
        "retry after a short delay",
        retryable=True,
        tool_call_id="call-2",
    )
    expected = (
        "error: provider unavailable\n"
        "hint: this error can be retried; decide from the context whether "
        "retrying is appropriate.\n"
        "reason: retry after a short delay"
    )

    # 重试信号只由 hint 行承载，不单独成行重复发送给模型。
    assert observation.content == expected
    message = _to_message(observation)
    assert message.get("content") == expected
    assert message.get("tool_call_id") == "call-2"
    assert "status" not in message


def test_non_retryable_error_factory_emits_do_not_retry_hint() -> None:
    observation = tool_error(
        "apply_patch",
        "invalid patch_write",
        "fix the patch_write format before calling again",
        retryable=False,
    )
    expected = (
        "error: invalid patch_write\n"
        "hint: do not retry this tool call.\n"
        "reason: fix the patch_write format before calling again"
    )

    assert observation.content == expected
    assert _to_message(observation).get("content") == expected


def test_error_factory_omits_blank_reason_and_falls_back_on_blank_error() -> None:
    observation = tool_error("apply_patch", "   ", "  ")

    assert observation.content == (
        "error: the tool call failed\nhint: do not retry this tool call."
    )


def test_projection_passes_raw_error_content_through_unchanged() -> None:
    message = _to_message(
        ToolObservation(tool_name="web_search", status="error", content="same error")
    )

    assert message.get("content") == "same error"


def test_cancelled_message_is_distinct_without_business_status_in_protocol() -> None:
    observation = tool_cancelled(
        "execute_terminal",
        tool_call_id="call-3",
    )
    message = _to_message(observation)

    expected = (
        "cancelled: the tool call was cancelled before completion; no result was produced."
    )
    assert observation.content == expected
    assert message.get("content") == expected
    assert "status" not in message
    assert "retryable" not in message.get("content")
