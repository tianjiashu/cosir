"""先固定输入与命令观察不混淆的工具边界。"""

import pytest
from pydantic import ValidationError

from app.core.tools.tool_models.terminal_session_args import TerminalReadArgs, TerminalWriteArgs
from app.service.terminal.errors import TerminalSessionError
from app.service.terminal.terminal_observation import TerminalObservation, json_size


def test_empty_line_submits_enter_and_preserves_whitespace():
    args = TerminalWriteArgs(session_id="s", operation_id="op", input={"type": "line", "text": ""})
    assert args.input.text == ""
    args = TerminalWriteArgs(
        session_id="s", operation_id="op", input={"type": "line", "text": "    print(1)  "}
    )
    assert args.input.text == "    print(1)  "


@pytest.mark.parametrize(
    "value",
    [
        {"type": "text", "text": "partial"},
        [{"type": "text", "text": "print(1)"}, {"type": "key", "key": "enter"}],
        {"type": "key", "key": "enter"},
        {"type": "key", "key": "tab"},
        {"type": "key", "key": "escape"},
        {"type": "key", "key": "backspace"},
    ],
)
def test_old_input_contract_is_rejected(value):
    with pytest.raises(ValidationError):
        TerminalWriteArgs(session_id="s", operation_id="op", input=value)


@pytest.mark.parametrize("text", ["a\nb", "a\r\nb", "a\r", "\t", "\x03", "\x1b", "\x7f", "\x85"])
def test_line_cannot_hide_newlines_or_control_keys(text):
    with pytest.raises(ValidationError):
        TerminalWriteArgs(session_id="s", operation_id="op", input={"type": "line", "text": text})


def test_wait_requires_a_named_command_and_no_implicit_command_guessing():
    with pytest.raises(ValidationError):
        TerminalReadArgs(session_id="s", wait={"until": "command_returned"})
    assert TerminalReadArgs(session_id="s").wait.until == "activity"


def test_only_read_accepts_an_optional_cursor():
    assert TerminalReadArgs(session_id="s").cursor is None
    assert TerminalReadArgs(session_id="s", cursor="g:2:0").cursor == "g:2:0"
    with pytest.raises(ValidationError, match="cursor"):
        TerminalWriteArgs(
            session_id="s", operation_id="op", input={"type": "line", "text": ""}, cursor="g:2:0"
        )


def test_input_byte_budget_includes_enter_and_utf8():
    base = {"session_id": "s", "operation_id": "op"}
    TerminalWriteArgs(**base, input={"type": "line", "text": "a" * 65535})
    for text in ["a" * 65536, "中" * 22000]:
        with pytest.raises(ValidationError):
            TerminalWriteArgs(**base, input={"type": "line", "text": text})


def test_interactive_input_output_cannot_complete_shell_command():
    observation = TerminalObservation("g", 10000, "auto")
    observation.shell_event({"kind": "command_started", "command": "python"})
    command = observation.active
    observation.output(">>> print(1)\r\n1\r\n>>> ")
    assert observation.active == command
    assert observation.commands[command] is None
    observation.shell_event(
        {
            "kind": "command_returned",
            "result": {"success": True, "code": 0, "code_kind": "shell_status"},
        }
    )
    assert observation.commands[command]["type"] == "command_returned"


def test_pagination_preserves_all_text_and_return_event():
    observation = TerminalObservation("g", 100000, "auto")
    observation.shell_event({"kind": "command_started", "command": "echo"})
    observation.output('中"\\\n' * 5000)
    observation.shell_event(
        {"kind": "command_returned", "result": {"success": True, "code": None, "code_kind": None}}
    )
    cursor = None
    events = []
    for _ in range(200):
        page = observation.page(cursor, {}, 600)
        assert json_size(page) <= 600
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert "".join(e["text"] for e in events if e["type"] == "output") == '中"\\\n' * 5000
    assert events[-1]["type"] == "command_returned"


def test_cursor_generation_gap_and_session_exit_are_explicit():
    observation = TerminalObservation("g", 150, "auto")
    observation.shell_event({"kind": "command_started", "command": "python"})
    command = observation.active
    observation.output("x" * 100)
    observation.end_tracking("shell_exited")
    page = observation.page("g:0:0", {}, 1000)
    assert page["gap"]
    assert observation.commands[command]["type"] == "tracking_ended"
    with pytest.raises(TerminalSessionError, match="generation"):
        observation.page("old:0:0", {}, 1000)


def test_structured_page_survives_global_tool_output_budget():
    import json

    from app.config.constant import Constant
    from app.core.tools.tool_execute.tool_observation_budget import ToolObservationBudget
    from app.core.tools.tool_execute.tool_success import tool_success

    observation = TerminalObservation("g", 100000, "auto")
    observation.output('中文"\\\n' * 5000)
    page = observation.page(None, {"session_id": "s"}, Constant.Tools.MAX_OUTPUT_CHARS - 256)
    content = "Terminal observation.\n" + json.dumps(
        page, ensure_ascii=False, separators=(",", ":")
    )
    result = tool_success("terminal_read", content=content)
    budgeted = ToolObservationBudget().apply(result, None)
    assert budgeted.content == content
    assert json.loads(budgeted.content.split("\n", 1)[1])["has_more"]


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "shell_prompt", "cwd": "/workspace"},
        {"kind": "unavailable", "reason": "integration_disabled"},
    ],
)
def test_lost_return_boundary_ends_tracking_without_success(event):
    observation = TerminalObservation("g", 10000, "auto")
    observation.shell_event({"kind": "command_started", "command": "python"})
    command = observation.active
    observation.shell_event(event)
    assert observation.active is None
    assert observation.commands[command]["type"] == "tracking_ended"
    assert "result" not in observation.commands[command]
