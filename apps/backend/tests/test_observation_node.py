"""``observe`` 节点与工具结果摘要的单测。

覆盖：错误计数重算、错误上限 RUN_FAILED 终态、race-lost、``last_tool_results`` 摘要
字段集与 ``content`` 截断；以及 ``edges`` 路由函数的语义（tools→observe、observe→model/END）。
"""

from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.graph import END

from app.core.workflows.nodes import observation_node as obs_mod
from app.core.workflows.nodes.tools_node import _build_tool_result_summaries
from app.core.workflows.react.edges import _after_observe, _after_tools, _should_continue
from app.core.workflows.react.state import ReactGraphState
from app.tools.schemas import ToolObservation


def _state(**overrides: Any) -> ReactGraphState:
    base: dict[str, Any] = {
        "step_count": 1,
        "tool_error_count": 0,
        "requested_tool": False,
        "final_response": False,
        "terminal": False,
        "pending_tool_calls": [],
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": [],
    }
    base.update(overrides)
    return ReactGraphState(**base)


def _result(status: str, *, error: str = "", content: str = "ok") -> dict[str, Any]:
    return {
        "call_id": "c1",
        "tool_name": "read_file",
        "status": status,
        "error": error,
        "reason": "",
        "content": content,
        "retryable": status != "success",
    }


def _fake_runtime_config(fail_return: Any) -> SimpleNamespace:
    """构造一个可被 observe 节点消费的伪 RuntimeConfig。

    ``fail_return`` 决定 ``fail_turn_if_running`` 的返回值：传入 turn 记录表示失败成功落定，
    传入 ``None`` 模拟 race-lost（turn 已非 running）。
    """
    turn = SimpleNamespace(turn_id="t1")
    operations = SimpleNamespace(
        get_current_turn=lambda: turn,
        fail_turn_if_running=lambda turn_id, end_reason: fail_return,
    )
    return SimpleNamespace(operations=operations, langfuse_trace_id="trace-1")


async def test_observe_no_error_keeps_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _fake_runtime_config(fail_return=SimpleNamespace(turn_id="t1"))
    captured: list[Any] = []
    monkeypatch.setattr(obs_mod, "_runtime_config", lambda: cfg)
    monkeypatch.setattr(obs_mod, "write_event", lambda *a, **k: captured.append(a))
    result = await obs_mod._observe_node(_state(last_tool_results=[_result("success")]))
    assert result == {"tool_error_count": 0}
    assert captured == []


async def test_observe_error_limit_triggers_run_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _fake_runtime_config(fail_return=SimpleNamespace(turn_id="t1"))
    captured: list[Any] = []
    monkeypatch.setattr(obs_mod, "_runtime_config", lambda: cfg)
    monkeypatch.setattr(obs_mod, "write_event", lambda *a, **k: captured.append(a))
    results = [_result("error", error="boom") for _ in range(3)]
    result = await obs_mod._observe_node(_state(last_tool_results=results))
    assert result == {"tool_error_count": 3, "terminal": True}
    assert len(captured) == 1
    # 事件参数为 (EventType.RUN_FAILED, RunFailedPayload)
    assert captured[0][0].value == "run_failed"
    assert captured[0][1].error == "tool_error_limit_reached"
    assert captured[0][1].tool_name == "read_file"


async def test_observe_error_limit_race_lost_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _fake_runtime_config(fail_return=None)  # race-lost
    captured: list[Any] = []
    monkeypatch.setattr(obs_mod, "_runtime_config", lambda: cfg)
    monkeypatch.setattr(obs_mod, "write_event", lambda *a, **k: captured.append(a))
    results = [_result("error", error="boom") for _ in range(3)]
    result = await obs_mod._observe_node(_state(last_tool_results=results))
    assert result == {"tool_error_count": 3, "terminal": True}
    assert captured == []  # race-lost 时跳过 RUN_FAILED 事件


def test_build_tool_result_summaries_fields_and_truncation() -> None:
    long_content = "x" * 5000
    observations = [
        ToolObservation(
            tool_call_id="c1",
            tool_name="read_file",
            status="success",
            content=long_content,
            error="",
            reason="",
            retryable=False,
        ),
        ToolObservation(
            tool_call_id="c2",
            tool_name="search_files",
            status="error",
            content="failed",
            error="boom",
            reason="check path",
            retryable=True,
        ),
    ]
    summaries = _build_tool_result_summaries(observations)
    assert len(summaries) == 2
    assert summaries[0]["call_id"] == "c1"
    assert summaries[0]["status"] == "success"
    assert summaries[0]["content"] == long_content[:4000]  # 截断到安全长度
    assert len(summaries[0]["content"]) == 4000
    assert summaries[1]["status"] == "error"
    assert summaries[1]["error"] == "boom"
    assert summaries[1]["reason"] == "check path"
    assert summaries[1]["retryable"] is True
    # 不承载 data 等大体积字段
    assert "data" not in summaries[0]


def test_after_tools_routes_to_observe_or_end() -> None:
    assert _after_tools(_state(terminal=False, requested_tool=True)) == "observe"
    assert _after_tools(_state(terminal=True)) == END
    assert _after_tools(_state(final_response=True)) == END


def test_after_observe_routes_to_model_or_end() -> None:
    assert _after_observe(_state(terminal=False)) == "model"
    assert _after_observe(_state(terminal=True)) == END
    assert _after_observe(_state(final_response=True)) == END


def test_should_continue_routes_to_tools_or_end() -> None:
    assert _should_continue(_state(requested_tool=True)) == "tools"
    assert _should_continue(_state(terminal=True)) == END


def test_state_has_no_messages_field() -> None:
    """回归防护：graph state 契约中不得再出现 ``messages`` 字段（消息由 RuntimeContext 独占）。"""
    assert "messages" not in ReactGraphState.model_fields
    assert "last_tool_results" in ReactGraphState.model_fields


def test_build_tool_result_summaries_carries_instruction() -> None:
    """模型调工具前的说明文本（instruction）必须随结果摘要下传，供 observe 节点排查。"""
    observations = [
        ToolObservation(
            tool_call_id="c1",
            tool_name="search_files",
            status="success",
            content="ok",
            error="",
            reason="",
            retryable=False,
        ),
        ToolObservation(
            tool_call_id="c2",
            tool_name="read_file",
            status="error",
            content="boom",
            error="missing",
            reason="path not found",
            retryable=False,
        ),
    ]
    instructions = {"c1": "先查文件结构", "c2": "再读入口文件"}
    summaries = _build_tool_result_summaries(observations, instructions)
    assert summaries[0]["instruction"] == "先查文件结构"
    assert summaries[1]["instruction"] == "再读入口文件"


def test_build_tool_result_summaries_instruction_default_empty() -> None:
    """未提供 instruction 映射时，摘要的 instruction 缺省为空串（向后兼容）。"""
    observations = [
        ToolObservation(
            tool_call_id="c1",
            tool_name="read_file",
            status="success",
            content="ok",
            error="",
            reason="",
            retryable=False,
        ),
    ]
    summaries = _build_tool_result_summaries(observations)
    assert summaries[0]["instruction"] == ""
    # 缺调用侧 instruction 映射（call_id 不在映射中）也应为空串
    summaries = _build_tool_result_summaries(observations, {"other": "x"})
    assert summaries[0]["instruction"] == ""


async def test_observe_error_limit_log_includes_instructions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """错误上限日志必须带出本批工具调用的 instruction，使排查能看到模型意图。"""
    import logging

    cfg = _fake_runtime_config(fail_return=SimpleNamespace(turn_id="t1"))
    monkeypatch.setattr(obs_mod, "_runtime_config", lambda: cfg)
    monkeypatch.setattr(obs_mod, "write_event", lambda *a, **k: None)
    results = [
        {**_result("error", error="boom"), "instruction": "先查文件结构"},
        {**_result("error", error="boom"), "instruction": "再读入口文件"},
        {**_result("error", error="boom"), "instruction": ""},
    ]
    with caplog.at_level(logging.WARNING):
        await obs_mod._observe_node(_state(last_tool_results=results))
    assert any(
        "observe_node_error_limit" in rec.message for rec in caplog.records
    )
    # 日志的 data 中 instructions 只收集非空项
    logged = [
        rec
        for rec in caplog.records
        if "observe_node_error_limit" in rec.message
    ]
    assert logged
    extra = getattr(logged[0], "data", None)
    assert extra is not None
    assert extra["instructions"] == ["先查文件结构", "再读入口文件"]


def test_build_tool_result_summaries_redacts_secret() -> None:
    """回归防护：摘要的 content 必须经过脱敏，避免明文凭据落盘 checkpoint。

    样例必须使用真实格式的 token（ghp_ 后跟 36 位、sk- 后跟 20 位以上），
    才能触发 ``redact_terminal_output`` 的正则匹配；畸形字符串不会被误脱敏。
    """
    secret_content = (
        "token is ghp_abcdefghijABCDEFGHIJabcdefghijabcdef and "  # noqa: S105 测试专用真实格式样例
        "key sk-abcdefghijABCDEFGHIJabcdefghij"
    )
    observations = [
        ToolObservation(
            tool_call_id="c1",
            tool_name="execute_terminal",
            status="success",
            content=secret_content,
            error="",
            reason="",
            retryable=False,
        ),
    ]
    summaries = _build_tool_result_summaries(observations)
    assert "ghp_abcdefghijABCDEFGHIJabcdefghijabcdef" not in summaries[0]["content"]
    assert "sk-abcdefghijABCDEFGHIJabcdefghij" not in summaries[0]["content"]
    assert "[REDACTED]" in summaries[0]["content"]
