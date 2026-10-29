"""终端工具面：5 个工具、按平台生成的按键 / 信号 schema、参数边界、展示数据与错误短提示。"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.workflow.message_queue.task_message import DecisionBody
from app.core.tool.background.deferred_registry import deferred_registry
from app.service import depends
from app.tools.terminal_runtime.errors import (
    TerminalIntegrationRequiredError,
    TerminalSessionStateError,
    TerminalUserCommandRunningError,
    TerminalUserTypingError,
)
from app.tools.terminal_runtime.shell_resolver import ShellResolver
from app.tools.tool_handler.terminal_session import (
    TerminalHandoffTool,
    TerminalListTool,
    TerminalReadTool,
    TerminalStartTool,
    TerminalWriteTool,
    build_terminal_definitions,
)
from app.tools.tool_handler.terminal_session.common import service_error_observation
from app.tools.tool_models import TerminalReadArgs, TerminalWriteArgs
from terminal_test_support import ReadyWorker, make_service, session_entries


def test_exactly_six_terminal_tools_are_built() -> None:
    definitions = {definition.name: definition for definition in build_terminal_definitions()}
    assert list(definitions) == [
        "terminal_list", "terminal_start", "terminal_read", "terminal_write",
        "terminal_handoff", "terminal_close",
    ]
    # 交接复用 HITL：只有它声明需要用户决定，决定之后重放原调用。
    assert definitions["terminal_handoff"].user_decision == "execute"
    assert all(
        definition.user_decision is None
        for name, definition in definitions.items()
        if name != "terminal_handoff"
    )


@pytest.mark.parametrize(
    ("system", "control", "signals"),
    [("Darwin", "ctrl_d", ["interrupt", "suspend", "eof"]), ("Windows", "ctrl_z", ["interrupt"])],
)
def test_write_schema_keys_and_signals_follow_host(monkeypatch, system, control, signals) -> None:
    monkeypatch.setattr("platform.system", lambda: system)
    schema = strict_parameters_schema(TerminalWriteArgs)
    keys = schema["$defs"]["InputKeys"]["properties"]["keys"]["items"]["enum"]
    assert control in keys and "shift_tab" in keys and "page_down" in keys
    assert schema["$defs"]["InputSignal"]["properties"]["signal"]["enum"] == signals
    other = "ctrl_z" if control == "ctrl_d" else "ctrl_d"
    with pytest.raises(ValidationError):
        TerminalWriteArgs.model_validate(
            {"session_id": "s", "operation_id": "o", "input": {"type": "keys", "keys": [other]}}
        )


def test_write_schema_is_strict_and_accepts_every_input_type() -> None:
    schema = strict_parameters_schema(TerminalWriteArgs)
    validator = Draft202012Validator(schema)
    wait = {"until": "match", "pattern": "ok", "quiet_ms": None, "timeout_ms": 30000}
    for input_value in (
        {"type": "line", "text": "ls"},
        {"type": "keys", "keys": ["tab"]},
        {"type": "signal", "signal": "interrupt"},
    ):
        validator.validate({"session_id": "s", "operation_id": "o", "input": input_value,
                            "wait": wait})
    assert "oneOf" not in json.dumps(schema) and "discriminator" not in json.dumps(schema)


@pytest.mark.parametrize(
    "bad",
    [
        {"input": {"type": "line", "text": "a\nb"}},
        {"input": {"type": "keys", "keys": []}},
        {"input": {"type": "key", "key": "ctrl_c"}},
        {"wait": {"until": "match"}},
        {"wait": {"until": "match", "pattern": "("}},
        {"wait": {"until": "elapsed", "timeout_ms": 3_600_001}},
    ],
)
def test_write_arguments_reject_invalid_input(bad: dict[str, Any]) -> None:
    raw = {"session_id": "s", "operation_id": "o", "input": {"type": "line", "text": "x"}, **bad}
    with pytest.raises(ValidationError):
        TerminalWriteArgs.model_validate(raw)


def test_read_arguments_bound_limits() -> None:
    assert TerminalReadArgs(session_id="s").tail is None
    with pytest.raises(ValidationError):
        TerminalReadArgs(session_id="s", limit=401)
    with pytest.raises(ValidationError):
        TerminalReadArgs(session_id="s", start_line=0)
    with pytest.raises(ValidationError):
        TerminalReadArgs.model_validate({"session_id": "s", "wait_ms": 3000})


def test_error_observations_use_controlled_status_hints() -> None:
    typing = service_error_observation("terminal_write", TerminalUserTypingError("typing"))
    assert typing.display_data["status_hint"] == "用户输入中"
    assert "Retry after" in (typing.content or "")
    busy = service_error_observation("terminal_write", TerminalUserCommandRunningError("busy"))
    assert busy.display_data["status_hint"] == "用户命令中"
    assert "Fix the session" in (busy.content or "")
    need = service_error_observation("terminal_write", TerminalIntegrationRequiredError("x"))
    assert need.display_data["status_hint"] == "缺少集成"
    other = service_error_observation("terminal_write", TerminalSessionStateError("x"))
    assert other.display_data["status_hint"] == "终端失败"


def test_shell_resolver_lists_installed_shells_with_integration(monkeypatch) -> None:
    installed = {"zsh", "bash", "pwsh"}
    monkeypatch.setattr(
        "app.tools.terminal_runtime.shell_resolver.shutil.which",
        lambda value: f"/bin/{value}" if value in installed else None,
    )
    shells = ShellResolver.available(system="Darwin", environ={"SHELL": "/bin/zsh"})
    assert [s["kind"] for s in shells] == ["zsh", "bash", "pwsh"]
    assert shells[0]["isDefault"] is True and shells[1]["isDefault"] is False
    assert [s["integrationSupported"] for s in shells] == [True, True, True]
    windows = ShellResolver.available(system="Windows", environ={})
    assert [s["kind"] for s in windows] == ["pwsh"]


@pytest.fixture
def tool_service(monkeypatch):
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    monkeypatch.setattr(depends, "get_terminal_session_service", lambda: service)
    monkeypatch.setattr(
        "app.tools.tool_handler.terminal_session.common.cancellation_registry",
        SimpleNamespace(is_cancelled=lambda task_id: False),
    )
    yield service, workers
    deferred_registry.drop_task(1)


def _context(tmp_path: Path) -> Any:
    return ToolExecutionContext(
        step_id="step-1",
        task_id=1,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=5,
        tool_call_id=f"call-{uuid.uuid4().hex}",
    )


def test_tool_handlers_return_observation_text_and_display_data(tool_service, tmp_path) -> None:
    service, workers = tool_service
    started = TerminalStartTool().execute(execution_context=_context(tmp_path), label="dev")
    assert started.display_data["kind"] == "terminal-session"
    assert started.display_data["label"] == "dev"
    session_id = str(started.display_data["session_id"])
    # 给模型的是一行摘要，不再是 JSON 元数据；集成就绪时不出现 integration 提示。
    assert started.content == f'session {session_id} "dev" by agent · zsh · running'

    workers[0].screen(["$ "], cursor=(0, 2))

    written = TerminalWriteTool().execute(
        session_id=session_id, operation_id="op", input={"type": "line", "text": "echo hi"},
        wait={"until": "elapsed", "pattern": None, "quiet_ms": None, "timeout_ms": 0},
        execution_context=_context(tmp_path),
    )
    assert written.status == "success" and not written.suspend
    assert (written.content or "").startswith(f"session {session_id} · zsh · running")
    assert written.display_data == {
        "kind": "terminal-session",
        "session_id": session_id,
        "status": "running",
        "lines": [1, 1],
        "screen": False,
        "wait_result": {"until": "elapsed", "end_reason": "elapsed"},
        "input_summary": "echo hi",
    }

    handoff_context = _context(tmp_path)
    handoff = TerminalHandoffTool().execute(
        session_id=session_id, reason="password", execution_context=handoff_context
    )
    # 首次调用是 HITL 等待占位：挂起 Agent，交接只登记在会话上。
    assert handoff.suspend is True and handoff.awaiting_decision is True
    assert handoff.display_data["input_summary"] == "password"
    entry = session_entries(service, 1)[0]
    assert entry["handoff"] == {"reason": "password", "toolCallId": handoff_context.tool_call_id}
    # 用户交还：决定到达后重放同一调用，报告交接以来的输出并清除交接。
    workers[0].screen(["$ ", "$ sudo ok"], cursor=(1, 9))
    returned = TerminalHandoffTool().execute(
        session_id=session_id,
        reason="password",
        execution_context=replace(
            handoff_context,
            user_decision=DecisionBody(
                tool_call_id=handoff_context.tool_call_id,
                tool_name="terminal_handoff",
                decision="approve",
            ),
        ),
    )
    assert returned.status == "success" and not returned.suspend
    assert "handed the terminal back" in (returned.content or "")
    assert session_entries(service, 1)[0]["handoff"] is None

    read = TerminalReadTool().execute(session_id=session_id, execution_context=_context(tmp_path))
    assert read.display_data["lines"] == [1, 2] and "input_summary" not in read.display_data

    listed = TerminalListTool().execute(execution_context=_context(tmp_path))
    session_line, shells_line = (listed.content or "").split("\n")
    assert session_line.startswith(f'session {session_id} "dev" by agent · zsh · running')
    assert shells_line.startswith("shells: ")
    assert listed.display_data == {"kind": "terminal-session", "session_count": 1}


def test_command_returned_without_integration_is_tool_error(monkeypatch, tmp_path) -> None:
    service, _ = make_service(monkeypatch)
    monkeypatch.setattr(depends, "get_terminal_session_service", lambda: service)
    monkeypatch.setattr(
        "app.tools.tool_handler.terminal_session.common.cancellation_registry",
        SimpleNamespace(is_cancelled=lambda task_id: False),
    )
    started = TerminalStartTool().execute(execution_context=_context(tmp_path))
    session_id = str(started.display_data["session_id"])
    result = TerminalWriteTool().execute(
        session_id=session_id, operation_id="op", input={"type": "line", "text": "ls"},
        wait={"until": "command_returned", "pattern": None, "quiet_ms": None, "timeout_ms": None},
        execution_context=_context(tmp_path),
    )
    assert result.status == "error"
    assert "TERMINAL_INTEGRATION_REQUIRED" in (result.content or "")


def test_handoff_replay_after_the_session_is_gone_reports_it(tool_service, tmp_path) -> None:
    """后端重启或会话被淘汰后才交还：会话已不在，结果照常让等待占位收尾。"""
    context = _context(tmp_path)
    returned = TerminalHandoffTool().execute(
        session_id="term_missing",
        reason="password",
        execution_context=replace(
            context,
            user_decision=DecisionBody(
                tool_call_id=context.tool_call_id, tool_name="terminal_handoff", decision="approve"
            ),
        ),
    )
    assert returned.status == "success"
    assert "no longer exists" in (returned.content or "")
