from app.core.tools.tool_handler.terminal_session import (
    TerminalCloseTool,
    TerminalReadTool,
    TerminalSignalTool,
    TerminalStartTool,
    TerminalWriteTool,
)
from app.core.tools.tool_handler.terminal_session.common import build_session_display_payload
from app.core.tools.tool_handler.terminal_session.write import encode_terminal_input
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import (
    TerminalCloseArgs,
    TerminalReadArgs,
    TerminalSignalArgs,
    TerminalStartArgs,
    TerminalWriteArgs,
)
from app.core.tools.tool_registry import ToolRegistry
from app.service.terminal.terminal_session_service import TerminalSessionService
from app.service.terminal.worker import ProcessTerminalWorker


def test_terminal_handlers_implement_handler_base_and_are_registered(monkeypatch) -> None:
    handlers = (
        TerminalStartTool,
        TerminalWriteTool,
        TerminalReadTool,
        TerminalSignalTool,
        TerminalCloseTool,
    )

    assert all(issubclass(handler, HandlerBase) for handler in handlers)
    monkeypatch.setattr(
        "app.core.tools.tool_handler.terminal_session.signal.platform.system",
        lambda: "Linux",
    )
    registry = ToolRegistry()
    for handler in handlers:
        registry.register(handler().to_definition_if_avaliable())
    registered_names = registry.get_all_tool_names()
    assert {
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
        "app.core.tools.tool_handler.terminal_session.signal.platform.system",
        lambda: "Windows",
    )
    windows_registry = ToolRegistry()
    for handler in handlers:
        windows_registry.register(handler().to_definition_if_avaliable())
    windows_registered_names = windows_registry.get_all_tool_names()
    assert "terminal_signal" not in windows_registered_names


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
    assert "identical input" in write["operation_id"]["description"]
    assert "submit=true" in write["data"]["description"]
    assert "real Enter" in write["submit"]["description"]
    assert "shell to exit" in write["wait_ms"]["description"]
    assert "next_seq minus 1" in write["after_seq"]["description"]

    read = TerminalReadArgs.model_json_schema()["properties"]
    assert "next_seq minus 1" in read["after_seq"]["description"]
    assert "skips one output frame" in read["after_seq"]["description"]
    assert "new output" in read["wait_ms"]["description"]

    signal = TerminalSignalArgs.model_json_schema()["properties"]
    assert "Ctrl-C-like" in signal["signal"]["description"]


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
    assert "does not advertise terminal signal support" in schema["properties"]["signal"][
        "description"
    ]


def test_terminal_definitions_snapshot_host_projection_at_construction(
    monkeypatch,
) -> None:
    """终端定义在构造时固化宿主投影，不依赖 ToolDefinition provider 热路径。"""

    handlers = (
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
    definitions = [handler().to_definition() for handler in handlers]
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
        "next_seq": 3,
        "initial_cwd": "H:/coding-agent",
        "shell_kind": "powershell",
    }


def test_terminal_write_submit_appends_real_enter_without_decoding_data() -> None:
    args = TerminalWriteArgs.model_validate(
        {
            "session_id": "session-1",
            "operation_id": "write-1",
            "data": "echo TERM-OK",
            "submit": True,
        }
    )
    assert args.submit is True
    assert encode_terminal_input("echo TERM-OK", submit=False) == b"echo TERM-OK"
    assert encode_terminal_input("echo TERM-OK", submit=True) == b"echo TERM-OK\r"
    assert encode_terminal_input(r"literal\\r\\n", submit=False) == b"literal\\\\r\\\\n"
    assert encode_terminal_input(r"literal\\r\\n", submit=True) == b"literal\\\\r\\\\n\r"


def test_terminal_write_schema_limits_final_utf8_bytes() -> None:
    base = {"session_id": "session-1", "operation_id": "write-1"}
    TerminalWriteArgs.model_validate({**base, "data": "a" * (64 * 1024 - 1), "submit": True})
    for payload in (
        {**base, "data": "a" * (64 * 1024), "submit": True},
        {**base, "data": "中" * (64 * 1024), "submit": False},
    ):
        try:
            TerminalWriteArgs.model_validate(payload)
        except ValueError:
            continue
        raise AssertionError("terminal input exceeding the final UTF-8 byte limit was accepted")
