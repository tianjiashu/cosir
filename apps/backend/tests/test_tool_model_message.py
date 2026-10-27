"""工具观察转模型可见 tool 消息的最小契约。

失败文本由调用点直接写进 ``tool_error`` 的 ``content``，取消文本由 ``tool_cancelled`` 拼装，
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


def test_error_factory_keeps_content_and_projection_passes_it_through() -> None:
    content = "provider unavailable. Retry after a short delay."
    observation = tool_error("web_search", content, tool_call_id="call-2")

    # 工厂不再套 error/hint/reason 模板，调用点写好的文本原样交给模型。
    assert observation.content == content
    message = _to_message(observation)
    assert message.get("content") == content
    assert message.get("tool_call_id") == "call-2"
    assert "status" not in message


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
