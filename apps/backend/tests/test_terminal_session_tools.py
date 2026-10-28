"""终端工具面：5 个工具、按平台生成的按键 / 信号 schema、参数边界、展示数据与错误短提示。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.workflow.wait.background_watches import background_watches
from app.service import depends
from app.tools.terminal_runtime.errors import (
    TerminalInputHeldByUserError,
    TerminalIntegrationRequiredError,
    TerminalSessionStateError,
)
from app.tools.terminal_runtime.shell_resolver import ShellResolver
from app.tools.tool_handler.terminal_session import (
    TerminalListTool,
    TerminalReadTool,
    TerminalStartTool,
    TerminalWriteTool,
    build_terminal_definitions,
)
from app.tools.tool_handler.terminal_session.common import service_error_observation
from app.tools.tool_models import TerminalReadArgs, TerminalWriteArgs
from terminal_test_support import ReadyWorker, make_service


def test_exactly_five_terminal_tools_are_built() -> None:
    names = [definition.name for definition in build_terminal_definitions()]
    assert names == [
        "terminal_list", "terminal_start", "terminal_read", "terminal_write", "terminal_close"
    ]


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
    wait = {"until": "match", "pattern": "ok", "quiet_ms": None, "timeout_ms": 60000}
    for input_value in (
        {"type": "line", "text": "ls"},
        {"type": "keys", "keys": ["tab"]},
        {"type": "signal", "signal": "interrupt"},
        {"type": "handoff", "reason": "password"},
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
    held = service_error_observation("terminal_write", TerminalInputHeldByUserError("typing"))
    assert held.display_data["status_hint"] == "用户输入中"
    assert "Retry after" in (held.content or "")
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
    background_watches.drop_task(1)


def _context(tmp_path: Path) -> Any:
    return SimpleNamespace(task_id=1, workspace_id=1, workspace_root=tmp_path, run_id=5)


def test_tool_handlers_return_observation_text_and_display_data(tool_service, tmp_path) -> None:
    service, workers = tool_service
    started = TerminalStartTool().execute(execution_context=_context(tmp_path), label="dev")
    meta = json.loads((started.content or "").split("\n", 1)[1])
    assert started.display_data["kind"] == "terminal-session"
    assert started.display_data["integration"] == "ready" and started.display_data["label"] == "dev"
    session_id = meta["session_id"]
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
        "wait_result": {"until": "elapsed", "end_reason": "elapsed"},
        "suspended": False,
        "input_type": "line",
        "input_summary": "echo hi",
    }

    handoff = TerminalWriteTool().execute(
        session_id=session_id, operation_id="op2",
        input={"type": "handoff", "reason": "password"}, wait=None,
        execution_context=_context(tmp_path),
    )
    assert handoff.suspend is True and handoff.display_data["input_type"] == "handoff"

    read = TerminalReadTool().execute(session_id=session_id, execution_context=_context(tmp_path))
    assert read.display_data["lines"] == [1, 1] and "input_type" not in read.display_data

    listed = TerminalListTool().execute(execution_context=_context(tmp_path))
    payload = json.loads((listed.content or "").split("\n", 1)[1])
    assert [s["session_id"] for s in payload["sessions"]] == [session_id]
    assert "available_shells" in payload
    assert listed.display_data == {"kind": "terminal-session", "session_count": 1}


def test_command_returned_without_integration_is_tool_error(monkeypatch, tmp_path) -> None:
    service, _ = make_service(monkeypatch)
    monkeypatch.setattr(depends, "get_terminal_session_service", lambda: service)
    monkeypatch.setattr(
        "app.tools.tool_handler.terminal_session.common.cancellation_registry",
        SimpleNamespace(is_cancelled=lambda task_id: False),
    )
    started = TerminalStartTool().execute(execution_context=_context(tmp_path))
    session_id = json.loads((started.content or "").split("\n", 1)[1])["session_id"]
    result = TerminalWriteTool().execute(
        session_id=session_id, operation_id="op", input={"type": "line", "text": "ls"},
        wait={"until": "command_returned", "pattern": None, "quiet_ms": None, "timeout_ms": None},
        execution_context=_context(tmp_path),
    )
    assert result.status == "error"
    assert "TERMINAL_INTEGRATION_REQUIRED" in (result.content or "")
