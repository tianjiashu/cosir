"""终端工具面：4 个工具（list / read / write / stop）、参数边界、展示数据、错误短提示与按 id 查找。"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from terminal_test_support import ReadyWorker, make_service, wait_until

from app.config.constant import Constant
from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL, TOOL_GROUP_TERMINAL_SESSION
from app.service import depends
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.terminal_runtime.command.running_command_registry import running_commands
from app.tools.terminal_runtime.errors import (
    TerminalIntegrationRequiredError,
    TerminalNotFoundError,
    TerminalPrefillRejectedError,
    TerminalSessionNotFoundError,
    TerminalSessionStateError,
)
from app.tools.terminal_runtime.session.shell_resolver import (
    available_session_shells,
    resolve_session_shell,
)
from app.tools.tool_handler.terminal import build_terminal_definitions
from app.tools.tool_handler.terminal.command_results import command_results
from app.tools.tool_handler.terminal.task_terminals import find_terminal
from app.tools.tool_handler.terminal.terminal_list import TerminalListTool
from app.tools.tool_handler.terminal.terminal_read import TerminalReadTool
from app.tools.tool_handler.terminal.terminal_stop import TerminalStopTool
from app.tools.tool_handler.terminal.terminal_write import TerminalWriteTool
from app.tools.tool_handler.terminal.tool_observation import with_terminal_errors
from app.tools.tool_models import TerminalReadArgs, TerminalStopArgs, TerminalWriteArgs


def test_exactly_four_terminal_tools_are_built() -> None:
    definitions = {definition.name: definition for definition in build_terminal_definitions()}
    assert list(definitions) == [
        "terminal_list", "terminal_read", "terminal_write", "terminal_stop",
    ]
    # 只有写入需要用户界面（用户终端），其余三个对子 Agent 与 Team 节点也开放。
    assert definitions["terminal_write"].requires_user_surface is True
    assert all(
        not definition.requires_user_surface
        for name, definition in definitions.items()
        if name != "terminal_write"
    )
    assert definitions["terminal_write"].group == TOOL_GROUP_TERMINAL_SESSION
    assert all(
        definitions[name].group == TOOL_GROUP_TERMINAL
        for name in ("terminal_list", "terminal_read", "terminal_stop")
    )
    assert all(definition.user_decision is None for definition in definitions.values())


def test_write_schema_is_strict_and_fills_without_running() -> None:
    schema = strict_parameters_schema(TerminalWriteArgs)
    assert set(schema["properties"]) == {"terminal_id", "command"}
    assert "It is not run" in schema["properties"]["command"]["description"]
    from jsonschema import Draft202012Validator

    Draft202012Validator(schema).validate({"terminal_id": "term_x", "command": "echo hi"})
    with pytest.raises(ValidationError):
        TerminalWriteArgs.model_validate({"terminal_id": "s", "command": "x", "input": {}})


def test_stop_schema_only_accepts_a_terminal_id() -> None:
    schema = strict_parameters_schema(TerminalStopArgs)
    assert set(schema["properties"]) == {"terminal_id"}
    assert TerminalStopArgs(terminal_id="call-1").terminal_id == "call-1"
    with pytest.raises(ValidationError):
        TerminalStopArgs.model_validate({"terminal_id": "s", "force": True})


def test_read_arguments_bound_limits() -> None:
    assert TerminalReadArgs(terminal_id="s").tail is None
    with pytest.raises(ValidationError):
        TerminalReadArgs(terminal_id="s", limit=401)
    with pytest.raises(ValidationError):
        TerminalReadArgs(terminal_id="s", start_line=0)
    with pytest.raises(ValidationError):
        TerminalReadArgs.model_validate({"terminal_id": "s", "wait_ms": 3000})


def _error_observation(tool_name: str, error: Exception) -> Any:
    """用一个必然失败的 action 走一遍工具层的领域错误归一化。"""

    def action() -> Any:
        raise error

    return with_terminal_errors(tool_name, action)


def test_error_observations_use_controlled_status_hints() -> None:
    rejected = _error_observation(
        "terminal_write", TerminalPrefillRejectedError("a command is running")
    )
    assert rejected.display_data["status_hint"] == "无法填入"
    assert "Give the command in your reply" in (rejected.content or "")
    need = _error_observation("terminal_read", TerminalIntegrationRequiredError("x"))
    assert need.display_data["status_hint"] == "缺少集成"
    assert "until=output_quiet" in (need.content or "")
    gone = _error_observation(
        "terminal_read", TerminalSessionNotFoundError("session s no longer exists")
    )
    assert "terminal_list" in (gone.content or "")
    assert "running commands" in (gone.content or "")
    other = _error_observation("terminal_read", TerminalSessionStateError("x"))
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
    shells = available_session_shells(system="Darwin", environ={"SHELL": "/bin/zsh"})
    # 顺序即 shell 目录的声明顺序，与 execute_terminal 的 enum 一致；默认项单独标记。
    assert [s["kind"] for s in shells] == ["sh", "bash", "zsh", "pwsh"]
    assert [s["isDefault"] for s in shells] == [False, False, True, False]
    assert [s["integrationSupported"] for s in shells] == [False, True, True, True]
    windows = available_session_shells(system="Windows", environ={})
    assert [s["kind"] for s in windows] == ["pwsh"]


def test_default_shell_outside_the_catalog_keeps_one_kind(monkeypatch) -> None:
    """``$SHELL`` 指向清单外的 shell：清单里的默认项与按它新建的会话是同一个 ``kind``。"""

    monkeypatch.setattr(
        "app.tools.shell.host_shells.shutil.which",
        lambda value: value if value == "/usr/local/bin/tcsh" else None,
    )
    environ = {"SHELL": "/usr/local/bin/tcsh"}
    shells = available_session_shells(system="Darwin", environ=environ)
    default = next(shell for shell in shells if shell["isDefault"])
    assert default == {
        "kind": "tcsh", "label": "tcsh", "isDefault": True, "integrationSupported": False,
    }
    assert resolve_session_shell("/usr/local/bin/tcsh", system="Darwin", environ=environ).kind == (
        "tcsh"
    )
    # 指向已知 shell 的路径按该 shell 处理，集成随之可用。
    monkeypatch.setattr(
        "app.tools.shell.host_shells.shutil.which", lambda value: "/opt/homebrew/bin/bash"
    )
    assert resolve_session_shell("/opt/homebrew/bin/bash", system="Darwin", environ={}).kind == (
        "bash"
    )


def test_user_default_shell_falls_back_to_the_account_login_shell(monkeypatch) -> None:
    """``$SHELL`` 未设置时取账户数据库里的登录 shell（交互终端 auto 与登录环境解析共用）。"""
    import pwd

    from app.tools.shell.host_shells import user_default_shell

    assert user_default_shell({"SHELL": " /bin/zsh "}) == "/bin/zsh"
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: type("P", (), {"pw_shell": "/bin/fish"}))
    assert user_default_shell({}) == "/bin/fish"
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: type("P", (), {"pw_shell": ""}))
    assert user_default_shell({}) == "/bin/sh"


@pytest.fixture
def tool_service(monkeypatch):
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    monkeypatch.setattr(depends, "get_terminal_session_service", lambda: service)
    # 只有等待期间轮询取消的两个工具读这个信号。
    for tool in ("terminal_read", "terminal_stop"):
        monkeypatch.setattr(
            f"app.tools.tool_handler.terminal.{tool}.should_stop_call",
            lambda task_id, tool_call_id: False,
        )
    yield service, workers
    command_results.drop_task(1)
    for command in running_commands.task_commands(1):
        running_commands.finish(1, command.terminal_id, None)


def _context(tmp_path: Path) -> Any:
    return ToolExecutionContext(
        step_id="step-1",
        task_id=1,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=5,
        tool_call_id=f"call-{uuid.uuid4().hex}",
    )


def _start_session(service: Any, workers: Any, tmp_path: Path) -> str:
    runtime = service.start(  # noqa: S604 - 显式 shell 取值，测试不启动真实 shell
        task_id=1, cwd=str(tmp_path), shell="auto"
    )
    workers[0].screen(["$ "], cursor=(0, 2))
    workers[0].shell("shell_prompt", cwd="/workspace")
    return runtime.session_id


def _start_running_command() -> str:
    running_commands.start(
        task_id=1,
        command_id="call-run",
        command="npm run dev",
        shell_kind="sh",
        cwd="/workspace",
    )
    return "call-run"


def test_tool_handlers_return_observation_text_and_display_data(tool_service, tmp_path) -> None:
    service, workers = tool_service
    session_id = _start_session(service, workers, tmp_path)

    filled = TerminalWriteTool().execute(
        terminal_id=session_id, command="echo hi", execution_context=_context(tmp_path)
    )
    assert filled.status == "success" and not filled.suspend
    assert workers[0].writes == [{"type": "prefill", "text": "echo hi"}]
    assert (filled.content or "").startswith(f"session {session_id} · zsh · running")
    assert "filled into the prompt" in (filled.content or "")
    assert filled.display_data == {
        "kind": "terminal-session",
        "terminal_id": session_id,
        "lines": [1, 1],
        "screen": False,
        "wait_result": None,
        "input_summary": "echo hi",
    }

    # 同一提示符上第二次填入是受控错误，短提示为「无法填入」。
    again = TerminalWriteTool().execute(
        terminal_id=session_id, command="echo again", execution_context=_context(tmp_path)
    )
    assert again.status == "error" and "TERMINAL_PREFILL_REJECTED" in (again.content or "")
    assert again.display_data["status_hint"] == "无法填入"

    read = TerminalReadTool().execute(
        terminal_id=session_id, execution_context=_context(tmp_path)
    )
    assert read.display_data["lines"] == [1, 1] and "input_summary" not in read.display_data

    listed = TerminalListTool().execute(execution_context=_context(tmp_path))
    [session_line] = (listed.content or "").split("\n")
    assert session_line.startswith(f"session {session_id} · zsh · running")
    assert listed.display_data == {"kind": "terminal-session", "terminal_count": 1}


def test_list_reports_no_terminals_when_nothing_is_open(tool_service, tmp_path) -> None:
    listed = TerminalListTool().execute(execution_context=_context(tmp_path))
    assert listed.content == "no terminals"
    assert listed.display_data == {"kind": "terminal-session", "terminal_count": 0}


def test_find_terminal_prefers_sessions_then_running_commands(tool_service, tmp_path) -> None:
    service, workers = tool_service
    session_id = _start_session(service, workers, tmp_path)
    session = find_terminal(1, session_id)
    assert session.terminal_id == session_id and session.snapshot().status == "running"

    command_id = _start_running_command()
    command = find_terminal(1, command_id)
    command.feed(["booting", "ready"], "")
    assert find_terminal(1, command_id) is command

    listed = TerminalListTool().execute(execution_context=_context(tmp_path))
    lines = (listed.content or "").split("\n")
    assert lines[0].startswith(f"session {session_id}")
    assert lines[1].startswith(f"command {command_id} · sh · running · executing `npm run dev`")
    assert listed.display_data == {"kind": "terminal-session", "terminal_count": 2}

    read = TerminalReadTool().execute(
        terminal_id=command_id, execution_context=_context(tmp_path)
    )
    assert "1│booting" in (read.content or "")
    assert (read.content or "").startswith(f"command {command_id} · sh · running")

    with pytest.raises(TerminalNotFoundError):
        find_terminal(1, "call-missing")


def test_stop_rejects_user_terminals_and_missing_commands(tool_service, tmp_path) -> None:
    service, workers = tool_service
    session_id = _start_session(service, workers, tmp_path)
    rejected = TerminalStopTool().execute(
        terminal_id=session_id, execution_context=_context(tmp_path)
    )
    assert rejected.status == "error"
    assert "is a terminal the user opened" in (rejected.content or "")

    missing = TerminalStopTool().execute(
        terminal_id="call-missing", execution_context=_context(tmp_path)
    )
    assert missing.status == "error"
    assert "no terminal call-missing" in (missing.content or "")


def test_stop_waits_for_the_command_to_exit(tool_service, tmp_path) -> None:
    command_id = _start_running_command()
    threading.Timer(
        0.1, lambda: running_commands.finish(1, command_id, ExecutionResult(end="cancelled"))
    ).start()

    stopped = TerminalStopTool().execute(
        terminal_id=command_id, execution_context=_context(tmp_path)
    )

    assert stopped.status == "success"
    assert f"stopped command {command_id}" in (stopped.content or "")
    wait_until(lambda: command_id not in [c.terminal_id for c in running_commands.task_commands(1)])


def test_stop_times_out_with_a_controlled_error(
    tool_service, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Constant.Terminal, "COMMAND_STOP_WAIT_SECONDS", 0.05)
    command_id = _start_running_command()

    stopped = TerminalStopTool().execute(
        terminal_id=command_id, execution_context=_context(tmp_path)
    )

    assert stopped.status == "error"
    assert "has not exited after" in (stopped.content or "")
