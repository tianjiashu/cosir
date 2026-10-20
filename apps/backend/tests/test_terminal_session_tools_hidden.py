import json
from types import SimpleNamespace

import pytest

from app.core.tools.display.terminal_display import build_session_display_payload
from app.core.tools.tool_handler.terminal_session import (
    TerminalCloseTool,
    TerminalListTool,
    TerminalReadTool,
    TerminalSignalTool,
    TerminalStartTool,
    TerminalWriteTool,
    build_terminal_signal_definition,
)
from app.core.tools.tool_handler.terminal_session.common import success_observation
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import (
    TerminalCloseArgs,
    TerminalReadArgs,
    TerminalSignalArgs,
    TerminalStartArgs,
    TerminalWriteArgs,
)
from app.core.tools.tool_registry import ToolRegistry
from app.service.terminal.process_terminal_worker import ProcessTerminalWorker
from app.service.terminal.terminal_session_service import TerminalSessionService


def test_terminal_handlers_implement_handler_base_and_are_registered(monkeypatch) -> None:
    handlers = (
        TerminalListTool,
        TerminalStartTool,
        TerminalWriteTool,
        TerminalReadTool,
        TerminalSignalTool,
        TerminalCloseTool,
    )

    assert all(issubclass(handler, HandlerBase) for handler in handlers)
    monkeypatch.setattr(
        "platform.system",
        lambda: "Linux",
    )
    registry = ToolRegistry()
    for handler in handlers:
        registry.register(handler().to_definition_if_avaliable())
    registered_names = registry.get_all_tool_names()
    assert {
        "terminal_list",
        "terminal_start",
        "terminal_read",
        "terminal_write",
        "terminal_signal",
        "terminal_close",
    }.issubset(registered_names)
    assert not hasattr(ProcessTerminalWorker, "resize")
    assert not hasattr(TerminalSessionService, "resize")
    assert "terminal_resize" not in {handler.name for handler in handlers}

    monkeypatch.setattr(
        "platform.system",
        lambda: "Windows",
    )
    windows_registry = ToolRegistry()
    for handler in handlers:
        windows_registry.register(handler().to_definition_if_avaliable())
    windows_registered_names = windows_registry.get_all_tool_names()
    assert "terminal_signal" in windows_registered_names


def test_terminal_start_schema_rejects_removed_dimensions() -> None:
    for payload in ({"cols": 20}, {"rows": 5}, {"cols": 20, "rows": 5}):
        try:
            TerminalStartArgs.model_validate(payload)
        except ValueError:
            continue
        raise AssertionError(f"invalid initial dimensions accepted: {payload}")


def test_terminal_session_schema_describes_agent_operational_semantics() -> None:
    models = (
        TerminalStartArgs,
        TerminalWriteArgs,
        TerminalReadArgs,
        TerminalSignalArgs,
        TerminalCloseArgs,
    )

    for model in models:
        properties = model.model_json_schema()["properties"]
        assert all(properties[name].get("description") for name in properties)
        assert all(len(properties[name]["description"]) <= 300 for name in properties)

    start = TerminalStartArgs.model_json_schema()["properties"]
    assert "PowerShell on Windows" in start["shell"]["description"]
    assert "cols" not in start
    assert "rows" not in start
    assert "inside it" in start["cwd"]["description"]

    write = TerminalWriteArgs.model_json_schema()["properties"]
    assert "input" in write and "cursor" not in write
    assert "data" not in write and "submit" not in write and "after_seq" not in write
    read = TerminalReadArgs.model_json_schema()["properties"]
    assert "wait_ms" in read and "cursor" not in read and "wait" not in read


def test_terminal_session_schema_describes_host_specific_shell_and_paths(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.core.tools.tool_handler.terminal_session.descriptions.platform.system",
        lambda: "Windows",
    )
    windows_schema = TerminalStartTool().to_definition().to_model_tool_definition()["parameters"]
    assert "Windows ConPTY" in windows_schema["properties"]["shell"]["description"]
    assert "Windows path syntax" in windows_schema["properties"]["cwd"]["description"]

    monkeypatch.setattr(
        "app.core.tools.tool_handler.terminal_session.descriptions.platform.system",
        lambda: "Darwin",
    )
    macos_schema = TerminalStartTool().to_definition().to_model_tool_definition()["parameters"]
    assert "macOS PTY" in macos_schema["properties"]["shell"]["description"]
    assert "zsh" in macos_schema["properties"]["shell"]["description"]

    monkeypatch.setattr(
        "app.core.tools.tool_handler.terminal_session.descriptions.platform.system",
        lambda: "Linux",
    )
    linux_schema = TerminalStartTool().to_definition().to_model_tool_definition()["parameters"]
    assert "Linux PTY" in linux_schema["properties"]["shell"]["description"]
    assert "bash" in linux_schema["properties"]["shell"]["description"]


def test_terminal_signal_schema_warns_about_windows_capability(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.core.tools.tool_handler.terminal_session.descriptions.platform.system",
        lambda: "Windows",
    )
    schema = TerminalSignalTool().to_definition().to_model_tool_definition()["parameters"]
    assert "supports interrupt only" in schema["properties"]["signal"]["description"]


def test_terminal_definitions_snapshot_host_projection_at_construction(
    monkeypatch,
) -> None:
    """终端定义在构造、注册时固化 strict 宿主投影，不在请求热路径重新生成。"""

    handlers = (
        TerminalListTool,
        TerminalStartTool,
        TerminalReadTool,
        TerminalWriteTool,
        TerminalSignalTool,
        TerminalCloseTool,
    )
    monkeypatch.setattr(
        "app.core.tools.tool_handler.terminal_session.descriptions.platform.system",
        lambda: "Windows",
    )
    definitions = [handler().to_definition().normalized() for handler in handlers]
    windows_payloads = [definition.to_model_tool_definition() for definition in definitions]

    for definition in definitions:
        assert definition.parameters_schema

    monkeypatch.setattr(
        "app.core.tools.tool_handler.terminal_session.descriptions.platform.system",
        lambda: "Darwin",
    )
    assert [definition.to_model_tool_definition() for definition in definitions] == windows_payloads


def test_terminal_session_display_variants_match_their_ui_roles() -> None:
    definitions = {
        handler.name: handler().to_definition()
        for handler in (
            TerminalListTool,
            TerminalStartTool,
            TerminalReadTool,
            TerminalWriteTool,
            TerminalSignalTool,
            TerminalCloseTool,
        )
    }

    assert definitions["terminal_start"].display.to_dict()["variant"] == "terminal-session-start"
    assert definitions["terminal_read"].display.to_dict()["variant"] == "terminal-session-read"
    assert definitions["terminal_write"].display.to_dict()["variant"] == "terminal-session-write"
    assert definitions["terminal_signal"].display.to_dict()["variant"] == "terminal-session-signal"
    assert definitions["terminal_close"].display.to_dict()["variant"] == "terminal-session-close"
    assert definitions["terminal_start"].display.to_dict()["surface"] == "standalone"
    assert definitions["terminal_read"].display.to_dict()["expandable"] is False
    assert definitions["terminal_write"].display.to_dict()["expandable"] is False
    assert definitions["terminal_read"].display.to_dict()["expand_layout"] == "none"
    assert definitions["terminal_write"].display.to_dict()["expand_layout"] == "none"


def test_session_display_payload_allowlists_ui_metadata() -> None:
    payload = build_session_display_payload(
        {
            "session_id": "term_demo",
            "status": "running",
            "generation": "gen_demo",
            "first_available_seq": 1,
            "next_seq": 3,
            "initial_cwd": "H:/coding-agent",
            "shell_kind": "powershell",
            "shell_executable": "powershell.exe",
            "worker_pid": 1234,
            "workspace_id": 99,
            "created_at": "secret-timestamp",
        },
        include_terminal_info=True,
    )

    assert payload == {
        "session_id": "term_demo",
        "status": "running",
        "generation": "gen_demo",
        "first_available_seq": 1,
        "initial_cwd": "H:/coding-agent",
        "shell_kind": "powershell",
    }


@pytest.mark.parametrize(
    "system, keys",
    [
        ("Darwin", ["ctrl_c", "ctrl_d"]),
        ("Linux", ["ctrl_c", "ctrl_d"]),
        ("Windows", ["ctrl_c", "ctrl_z"]),
    ],
)
def test_write_schema_declares_only_host_control_keys(monkeypatch, system, keys):
    monkeypatch.setattr(
        "app.core.tools.tool_handler.terminal_session.descriptions.platform.system", lambda: system
    )
    schema = TerminalWriteTool().to_definition().to_model_tool_definition()["parameters"]
    assert schema["$defs"]["InputKey"]["properties"]["key"]["enum"] == keys
    assert "InputText" not in schema["$defs"]
    assert "InputLine" in schema["$defs"]
    assert "items" not in schema["properties"]["input"]


def test_windows_signal_builder_registers_interrupt_capable_tool(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    definition = build_terminal_signal_definition()
    assert definition.name == "terminal_signal"
    assert "Windows worker supports interrupt" in definition.description


def test_agent_projection_removes_only_preview_watermarks_and_preserves_display():
    payload = {
        "session_id": "s",
        "first_available_seq": 1,
        "next_seq": 4,
        "history": "[1] tail",
    }
    display = {"kind": "terminal-session", "first_available_seq": 1}
    observation = success_observation(
        "terminal_read", payload, summary="read", display_payload=display
    )
    content = json.loads(observation.content.split("\n", 1)[1])
    assert content == {"session_id": "s", "history": payload["history"]}
    assert observation.display_data == display
    assert payload["first_available_seq"] == 1 and payload["next_seq"] == 4


def test_terminal_list_projects_each_session_without_mutating_service_snapshots():
    snapshots = [{"session_id": "s", "first_available_seq": 1, "next_seq": 4}]
    service = SimpleNamespace(list_sessions=lambda **kwargs: snapshots)
    context = SimpleNamespace(
        task_id=1,
        workspace_id=2,
        run_id=3,
        runtime_dependencies=SimpleNamespace(
            terminal_session_service=service
        ),
    )
    observation = TerminalListTool().execute(context)
    content = json.loads(observation.content.split("\n", 1)[1])
    assert content == {"sessions": [{"session_id": "s"}]}
    assert snapshots[0]["first_available_seq"] == 1 and snapshots[0]["next_seq"] == 4


def test_repeated_read_error_explains_recovery_without_claiming_command_failure():
    from app.core.tools.tool_handler.terminal_session.common import with_terminal_errors
    from app.service.terminal.terminal_read_guard import TerminalReadGuard

    guard = TerminalReadGuard()
    for _ in range(5):
        guard.observe(1, (1, "running"))
    result = with_terminal_errors("terminal_read", lambda: guard.observe(1, (1, "running")))
    assert result.status == "error" and result.retryable
    assert "TERMINAL_READ_UNCHANGED" in result.error
    assert "does not stop, fail, or otherwise affect the command or session" in result.error
    assert "terminal operation failed" not in result.error
    assert "does not reset the counter or guarantee recovery" in result.reason
    assert "new output or state changes" in result.reason
    assert result.display_data == {"status_hint": "重复读取"}
    assert result.content is None
    # 错误映射只改提示，下一次观察到真实进展即恢复。
    assert guard.observe(1, (2, "running")) is None


def test_other_terminal_errors_keep_the_existing_error_projection():
    from app.core.tools.tool_handler.terminal_session.common import service_error_observation
    from app.service.terminal.errors import TerminalReadInProgressError, TerminalSessionStateError

    for error in [
        TerminalReadInProgressError("already waiting"),
        TerminalSessionStateError("closed"),
    ]:
        result = service_error_observation("terminal_read", error)
        assert result.status == "error"
        assert result.retryable == error.retryable
        assert result.error == f"terminal operation failed: {error}"
        assert result.display_data == {"status_hint": "终端失败"}
