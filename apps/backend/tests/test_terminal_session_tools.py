"""终端工具面：3 个工具（list / read / write）、参数边界、展示数据与错误短提示。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from terminal_test_support import ReadyWorker, make_service

from app.core.tool.background.background_registry import background_registry
from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.service import depends
from app.tools.terminal_runtime.errors import (
    TerminalIntegrationRequiredError,
    TerminalPrefillRejectedError,
    TerminalSessionNotFoundError,
    TerminalSessionStateError,
)
from app.tools.terminal_runtime.shell_resolver import ShellResolver
from app.tools.tool_handler.terminal_session import (
    TerminalListTool,
    TerminalReadTool,
    TerminalWriteTool,
    build_terminal_definitions,
)
from app.tools.tool_handler.terminal_session.common import with_terminal_errors
from app.tools.tool_models import TerminalReadArgs, TerminalWriteArgs


def test_exactly_three_terminal_tools_are_built() -> None:
    definitions = {definition.name: definition for definition in build_terminal_definitions()}
    assert list(definitions) == ["terminal_list", "terminal_read", "terminal_write"]
    # Agent 对交互终端没有任何 HITL 工具：填入不执行，也不需要用户交还。
    assert all(definition.user_decision is None for definition in definitions.values())


def test_write_schema_is_strict_and_fills_without_running() -> None:
    schema = strict_parameters_schema(TerminalWriteArgs)
    assert set(schema["properties"]) == {"session_id", "command"}
    assert "It is not run" in schema["properties"]["command"]["description"]
    from jsonschema import Draft202012Validator

    Draft202012Validator(schema).validate({"session_id": "s", "command": "echo hi"})
    with pytest.raises(ValidationError):
        TerminalWriteArgs.model_validate({"session_id": "s", "command": "x", "input": {}})


def test_read_arguments_bound_limits() -> None:
    assert TerminalReadArgs(session_id="s").tail is None
    with pytest.raises(ValidationError):
        TerminalReadArgs(session_id="s", limit=401)
    with pytest.raises(ValidationError):
        TerminalReadArgs(session_id="s", start_line=0)
    with pytest.raises(ValidationError):
        TerminalReadArgs.model_validate({"session_id": "s", "wait_ms": 3000})


def test_error_observations_use_controlled_status_hints() -> None:
    rejected = _observe(
        "terminal_write", TerminalPrefillRejectedError("a command is running")
    )
    assert rejected.display_data["status_hint"] == "无法填入"
    assert "Give the command in your reply" in (rejected.content or "")
    need = _observe("terminal_read", TerminalIntegrationRequiredError("x"))
    assert need.display_data["status_hint"] == "缺少集成"
    assert "until=output_quiet" in (need.content or "")
    gone = _observe(
        "terminal_read", TerminalSessionNotFoundError("session s no longer exists")
    )
    assert "terminal_list" in (gone.content or "")
    other = _observe("terminal_read", TerminalSessionStateError("x"))
    assert other.display_data["status_hint"] == "终端失败"
    # 多因的 StateError 不追加类型级建议：message 已是完整事实。
    assert (other.content or "").endswith(": x")


def test_shell_resolver_lists_installed_shells_with_integration(monkeypatch) -> None:
    # Windows 按 <shell>.exe 定位，POSIX 按名字定位（见 host_shells.find_shell）；与真实
    # shutil.which 一致，传入已安装 shell 的路径（$SHELL）时原样返回。
    installed = {"zsh", "bash", "pwsh", "pwsh.exe"}

    def which(value: str) -> str | None:
        name = value.removeprefix("/bin/")
        return f"/bin/{name}" if name in installed else None

    monkeypatch.setattr("app.tools.shell.host_shells.shutil.which", which)
    # /bin/sh 按固定路径探测，不经 PATH。
    monkeypatch.setattr("app.tools.shell.host_shells.os.path.isfile", lambda _path: True)
    monkeypatch.setattr("app.tools.shell.host_shells.os.access", lambda *_a, **_k: True)
    shells = ShellResolver.available(system="Darwin", environ={"SHELL": "/bin/zsh"})
    # 顺序即 shell 目录的声明顺序，与 execute_terminal 的 enum 一致；默认项单独标记。
    assert [s["kind"] for s in shells] == ["sh", "bash", "zsh", "pwsh"]
    assert [s["isDefault"] for s in shells] == [False, False, True, False]
    assert [s["integrationSupported"] for s in shells] == [False, True, True, True]
    windows = ShellResolver.available(system="Windows", environ={})
    assert [s["kind"] for s in windows] == ["pwsh"]


@pytest.fixture
def tool_service(monkeypatch):
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    monkeypatch.setattr(depends, "get_terminal_session_service", lambda: service)
    monkeypatch.setattr(
        "app.tools.tool_handler.terminal_session.common.should_stop_call",
        lambda task_id, tool_call_id: False,
    )
    yield service, workers
    background_registry.drop_task(1)


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
    runtime = service.start(  # noqa: S604 - 显式 shell 取值，测试不启动真实 shell
        task_id=1, workspace_root=str(tmp_path), shell="auto"
    )
    session_id = runtime.session_id
    workers[0].screen(["$ "], cursor=(0, 2))
    workers[0].shell("shell_prompt", cwd="/w")

    filled = TerminalWriteTool().execute(
        session_id=session_id, command="echo hi", execution_context=_context(tmp_path)
    )
    assert filled.status == "success" and not filled.suspend
    assert workers[0].writes == [{"type": "prefill", "text": "echo hi"}]
    assert (filled.content or "").startswith(f"session {session_id} · zsh · running")
    assert "filled into the prompt" in (filled.content or "")
    assert filled.display_data == {
        "kind": "terminal-session",
        "session_id": session_id,
        "lines": [1, 1],
        "screen": False,
        "wait_result": None,
        "input_summary": "echo hi",
    }

    # 同一提示符上第二次填入是受控错误，短提示为「无法填入」。
    again = TerminalWriteTool().execute(
        session_id=session_id, command="echo again", execution_context=_context(tmp_path)
    )
    assert again.status == "error" and "TERMINAL_PREFILL_REJECTED" in (again.content or "")
    assert again.display_data["status_hint"] == "无法填入"

    read = TerminalReadTool().execute(session_id=session_id, execution_context=_context(tmp_path))
    assert read.display_data["lines"] == [1, 1] and "input_summary" not in read.display_data

    listed = TerminalListTool().execute(execution_context=_context(tmp_path))
    assert (listed.content or "").startswith(f"session {session_id} · zsh · running")
    assert "shells:" not in (listed.content or "")
    assert listed.display_data == {"kind": "terminal-session", "session_count": 1}


def test_command_returned_without_integration_is_tool_error(monkeypatch, tmp_path) -> None:
    service, _ = make_service(monkeypatch)
    monkeypatch.setattr(depends, "get_terminal_session_service", lambda: service)
    monkeypatch.setattr(
        "app.tools.tool_handler.terminal_session.common.should_stop_call",
        lambda task_id, tool_call_id: False,
    )
    runtime = service.start(  # noqa: S604 - 显式 shell 取值，测试不启动真实 shell
        task_id=1, workspace_root=str(tmp_path), shell="auto"
    )
    result = TerminalReadTool().execute(
        session_id=runtime.session_id,
        wait={"until": "command_returned", "pattern": None, "quiet_ms": None, "timeout_ms": None},
        execution_context=_context(tmp_path),
    )
    assert result.status == "error"
    assert "TERMINAL_INTEGRATION_REQUIRED" in (result.content or "")


def _observe(tool_name: str, exc: Exception):
    """经工具入口共用的错误归一化（``with_terminal_errors``）把领域错误投影成观察。"""

    def fail():
        raise exc

    return with_terminal_errors(tool_name, fail)
