"""``tools`` 节点单测：恢复执行时日志必须带出模型调工具前的 instruction。"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.workflows.nodes import tools_node as tools_mod
from app.core.workflows.react.state import ReactGraphState


def _state(**overrides: Any) -> ReactGraphState:
    base: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "requested_tool": True,
        "final_response": False,
        "terminal": False,
        "pending_tool_calls": [],
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": [],
    }
    base.update(overrides)
    return ReactGraphState(**base)


def _fake_runtime_config(
    *,
    tool_calls: list[dict[str, Any]],
    observations: list[Any],
) -> SimpleNamespace:
    """构造可被 tools 节点消费的伪 RuntimeConfig。

    ``tool_calls`` 为待执行调用（含 instruction）；``observations`` 为工具执行结果桩。
    ``approval_resolver`` 置 None 模拟自动放行，避免 interrupt 暂停 graph。
    """
    turn = SimpleNamespace(turn_id="t1")

    def _run_tool_calls(*_a: Any, **_k: Any) -> SimpleNamespace:
        return SimpleNamespace(
            observations=observations,
            messages_for_model=[
                SimpleNamespace(
                    role="tool",
                    content_text=None,
                    metadata={"tool_call_id": "call-1"},
                )
            ],
        )

    operations = SimpleNamespace(
        is_current_turn_cancelled=lambda: False,
        get_current_task=lambda: SimpleNamespace(task_id="task-1"),
        get_current_turn=lambda: turn,
        run_tool_calls=_run_tool_calls,
    )
    return SimpleNamespace(
        operations=operations,
        turn=turn,
        approval_resolver=None,  # 自动放行
    )


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, cfg: SimpleNamespace) -> None:
    monkeypatch.setattr(tools_mod, "_runtime_config", lambda: cfg)
    monkeypatch.setattr(tools_mod, "_runtime_context", lambda: SimpleNamespace(
        add_message=lambda _m: None,
    ))
    monkeypatch.setattr(tools_mod, "_make_write_event", lambda: (lambda *a, **k: None))
    # instruction 透传与 resumed 日志是本次验证目标；持久化落库属内部细节，用 no-op 桩隔离，
    # 避免为日志测试重建整套 RuntimeMessage 桩。
    monkeypatch.setattr(tools_mod, "_persist_tool_observations", lambda *a, **k: None)


async def test_tools_node_resumed_log_carries_instruction(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """工具节点恢复执行日志必须带出 instruction，使排查能看到模型意图。"""
    import logging

    from app.tools.schemas import ToolObservation

    tool_calls = [
        {
            "tool_name": "search_files",
            "arguments": {"pattern": "*.py"},
            "call_id": "call-1",
            "instruction": "先查文件结构",
        }
    ]
    observations = [
        ToolObservation(
            tool_call_id="call-1",
            tool_name="search_files",
            status="success",
            content="ok",
            error="",
            reason="",
            retryable=False,
        )
    ]
    cfg = _fake_runtime_config(tool_calls=tool_calls, observations=observations)
    _patch_runtime(monkeypatch, cfg)
    with caplog.at_level(logging.INFO):
        await tools_mod._tools_node(_state(pending_tool_calls=tool_calls))
    logged = [r for r in caplog.records if "tools_node_resumed" in r.message]
    assert logged
    extra = getattr(logged[0], "data", None)
    assert extra is not None
    assert extra["instruction"] == "先查文件结构"


async def test_tools_node_resumed_log_redacts_instruction_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """resumed 日志的 instruction 必须脱敏，避免说明文本含凭据落日志。"""
    import logging

    from app.tools.schemas import ToolObservation

    tool_calls = [
        {
            "tool_name": "execute_terminal",
            "arguments": {"command": "x"},
            "call_id": "call-1",
            "instruction": "用密码 sk-1234567890abcdefghij 执行",
        }
    ]
    observations = [
        ToolObservation(
            tool_call_id="call-1",
            tool_name="execute_terminal",
            status="success",
            content="ok",
            error="",
            reason="",
            retryable=False,
        )
    ]
    cfg = _fake_runtime_config(tool_calls=tool_calls, observations=observations)
    _patch_runtime(monkeypatch, cfg)
    with caplog.at_level(logging.INFO):
        await tools_mod._tools_node(_state(pending_tool_calls=tool_calls))
    logged = [r for r in caplog.records if "tools_node_resumed" in r.message]
    extra = getattr(logged[0], "data", None)
    assert "sk-1234567890abcdef" not in extra["instruction"]
    assert "[REDACTED]" in extra["instruction"]
