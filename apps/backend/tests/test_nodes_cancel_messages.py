"""``_tools_node`` 取消分支的协议闭合测试。

验证在 turn 被取消的场景下，节点不会丢弃工具响应消息，从而避免 checkpoint 残留
悬空 assistant（带 tool_calls 但无配对 ToolMessage），进而防止下一轮模型调用触发
OpenAI 协议校验失败（"assistant message with tool_calls must be followed by tool
messages"）。
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.core.workflows.react.nodes import _tools_node
from app.core.workflows.react.state import ReactGraphState
from app.models import RuntimeMessage
from app.service.tool_execution.run_result import ToolRunResult


def _make_state(pending_tool_calls: list[dict] | None = None) -> ReactGraphState:
    """构造最小可用的 ``ReactGraphState`` 实例（仅填充节点执行所需字段）。"""
    return ReactGraphState(
        messages=[],
        step_count=1,
        tool_error_count=0,
        requested_tool=True,
        final_response=False,
        terminal=False,
        pending_tool_calls=pending_tool_calls or [],
        max_steps=10,
        final_text="",
    )


def _make_rc(operations: object) -> SimpleNamespace:
    """构造 ``RuntimeConfig`` 等价 stub（只需节点读取的字段）。"""
    return SimpleNamespace(
        operations=operations,
        task=SimpleNamespace(task_id="task-1"),
        turn=SimpleNamespace(turn_id="turn-1"),
        approval_resolver=None,
    )


async def test_tools_node_cancelled_after_execution_keeps_tool_messages():
    """执行后取消：执行前未取消、执行后才取消，已产生的工具响应消息必须回写。"""
    tool_message = RuntimeMessage(
        role="tool",
        content_text='{"content": "cancelled"}',
        metadata={"tool_call_id": "call-1"},
    )
    tool_run = ToolRunResult(observations=[], messages_for_model=[tool_message])

    # 第一次检查（执行前）未取消，第二次检查（执行后）已取消
    call_flags = {"before": False}

    def is_cancelled() -> bool:
        if not call_flags["before"]:
            call_flags["before"] = True
            return False
        return True

    operations = SimpleNamespace(
        is_current_turn_cancelled=is_cancelled,
        run_tool_calls=lambda *args, **kwargs: tool_run,
    )
    state = _make_state(pending_tool_calls=[{"id": "call-1", "tool_name": "execute_terminal"}])

    with (
        patch(
            "app.core.workflows.react.nodes._runtime_config",
            return_value=_make_rc(operations),
        ),
        patch(
            "app.core.workflows.react.nodes.get_stream_writer",
            return_value=lambda *a, **k: None,
        ),
    ):
        result = await _tools_node(state)

    assert result["terminal"] is True
    assert result["messages"] == [tool_message]


async def test_tools_node_cancelled_before_execution_emits_placeholder():
    """执行前取消：尚未执行的 tool_calls 必须补占位 ToolMessage 闭合协议。"""
    operations = SimpleNamespace(
        is_current_turn_cancelled=lambda: True,
        run_tool_calls=lambda **kwargs: ToolRunResult(observations=[], messages_for_model=[]),
    )
    pending = [
        {"id": "call-1", "tool_name": "execute_terminal"},
        {"id": "", "tool_name": "read_file"},  # 空 id 不应生成占位
    ]
    state = _make_state(pending_tool_calls=pending)

    with patch(
        "app.core.workflows.react.nodes._runtime_config",
        return_value=_make_rc(operations),
    ):
        result = await _tools_node(state)

    assert result["terminal"] is True
    assert len(result["messages"]) == 1
    assert result["messages"][0].metadata.get("tool_call_id") == "call-1"
