"""先固定输入与命令观察不混淆的工具边界。"""

import pytest
from pydantic import ValidationError

from app.tools.tool_models.terminal_session_args import TerminalReadArgs, TerminalWriteArgs
from app.core.tool.validation.arguments import validate_tool_arguments
from app.service.terminal.terminal_observation import TerminalObservation, json_size


@pytest.mark.parametrize(
    "system, allowed, rejected",
    [
        ("Darwin", ["ctrl_c", "ctrl_d"], "ctrl_z"),
        ("Linux", ["ctrl_c", "ctrl_d"], "ctrl_z"),
        ("Windows", ["ctrl_c", "ctrl_z"], "ctrl_d"),
    ],
)
def test_host_key_schema_and_actual_argument_validation_agree(
    monkeypatch, system, allowed, rejected
):
    monkeypatch.setattr("platform.system", lambda: system)
    schema = TerminalWriteArgs.model_json_schema()
    assert schema["$defs"]["InputKey"]["properties"]["key"]["enum"] == allowed
    for key in [*allowed, rejected]:
        args = {"session_id": "s", "operation_id": "op", "input": {"type": "key", "key": key}}
        result = validate_tool_arguments(args, schema, TerminalWriteArgs)
        assert result.ok == (key in allowed)
        if key == rejected:
            assert "input.InputKey.key" in result.error
            assert system in result.error and rejected in result.error


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


@pytest.mark.parametrize(
    "payload",
    [{"cursor": "old"}, {"wait": {"until": "activity"}}, {"wait_ms": 2999}, {"wait_ms": 30001}],
)
def test_read_rejects_old_contract_and_enforces_wait_floor(payload):
    with pytest.raises(ValidationError):
        TerminalReadArgs(session_id="s", **payload)
    assert TerminalReadArgs(session_id="s").wait_ms == 3000
    assert TerminalReadArgs(session_id="s", wait_ms=30000).wait_ms == 30000


def test_input_byte_budget_includes_enter_and_utf8():
    base = {"session_id": "s", "operation_id": "op"}
    TerminalWriteArgs(**base, input={"type": "line", "text": "a" * 65535})
    for text in ["a" * 65536, "中" * 22000]:
        with pytest.raises(ValidationError):
            TerminalWriteArgs(**base, input={"type": "line", "text": text})


def make_observation(limit=100000):
    observation = TerminalObservation("g", limit, "auto")
    observation.initialize_screen({"rows": 24, "columns": 80})
    return observation


def test_snapshot_retains_latest_history_and_command_without_pagination():
    observation = make_observation(100)
    observation.shell_event({"kind": "command_started", "command": "python"})
    command = observation.active
    observation.history("old" * 500 + "NEWEST\n")
    assert observation.active == command
    assert "result" not in observation.latest_command
    observation.shell_event(
        {
            "kind": "command_returned",
            "result": {"success": True, "code": 0, "code_kind": "shell_status"},
        }
    )
    page = observation.snapshot({}, 1000)
    assert page["history"].endswith("NEWEST\n")
    assert page["history_truncated"]
    assert page["latest_command"]["command_id"] == command
    assert page["latest_command"]["status"] == "returned"
    assert not {"next_cursor", "has_more", "gap", "events"} & page.keys()
    assert observation.snapshot({}, 1000) == page
    page["latest_command"]["result"]["success"] = False
    assert observation.latest_command["result"]["success"] is True


def test_snapshot_budget_keeps_latest_screen_and_tail_without_corrupting_json():
    import json

    from app.config.constant import Constant
    from app.core.tool.execute.tool_observation_budget import ToolObservationBudget
    from app.core.tool.results.tool_success import tool_success

    observation = make_observation()
    observation.update_screen(
        {
            **observation.screen,
            "lines": [
                {"text": "old" * 10000, "wrapped": True},
                {"text": "LATEST QUESTION?", "wrapped": False},
            ],
        },
        ['中文"\\\n' * 10000 + "LATEST HISTORY"],
    )
    page = observation.snapshot({}, 4000)
    assert json_size(page) <= 4000
    assert page["screen"]["text"] == "LATEST QUESTION?"
    assert page["screen"]["row_offset"] == 1
    assert page["screen"]["truncated"]
    assert page["history"].endswith("LATEST HISTORY")
    assert page["history_truncated"]
    large = observation.snapshot({}, Constant.Tools.MAX_OUTPUT_CHARS - 1024)
    content = "Terminal observation.\n" + json.dumps(
        large, ensure_ascii=False, separators=(",", ":")
    )
    budgeted = ToolObservationBudget().apply(tool_success("terminal_read", content=content), None)
    assert budgeted.content == content


def test_single_oversized_line_keeps_suffix_and_explicit_omission():
    observation = make_observation()
    observation.update_screen(
        {
            **observation.screen,
            "lines": [{"text": "e" + "\u0301" * 20000 + "ANSWER?", "wrapped": False}],
        },
        [],
    )
    page = observation.snapshot({}, 2000)
    assert json_size(page) <= 2000
    assert page["screen"]["text"].endswith("ANSWER?")
    assert page["screen"]["first_row_omitted_chars"] > 0
    assert page["screen"]["truncated"]


def test_compact_screen_preserves_unicode_wraps_blank_rows_and_clear():
    observation = make_observation()
    observation.update_screen(
        {
            **observation.screen,
            "lines": [
                {"text": "  中e\u0301", "wrapped": True},
                {"text": "", "wrapped": False},
                {"text": "last ", "wrapped": False},
                {"text": "", "wrapped": False},
            ],
        },
        [],
    )
    first = observation.snapshot({}, 4000)
    assert first["screen"]["text"] == "  中e\u0301\n\nlast "
    assert first["screen"]["wrapped_rows"] == [0]
    first["screen"]["cursor"]["row"] = 99
    assert observation.screen["cursor"]["row"] == 0
    observation.update_screen({**observation.screen, "lines": []}, [])
    assert observation.snapshot({}, 4000)["screen"]["text"] == ""


@pytest.mark.parametrize(
    "event",
    [{"kind": "shell_prompt", "cwd": "/workspace"}, {"kind": "unavailable", "reason": "disabled"}],
)
def test_lost_command_boundary_is_unknown_not_success(event):
    observation = make_observation()
    observation.shell_event({"kind": "command_started", "command": "python"})
    observation.shell_event(event)
    assert observation.latest_command["status"] == "tracking_ended"
    assert "result" not in observation.latest_command
    assert observation.active is None


def test_new_command_replaces_previous_result():
    observation = make_observation()
    observation.shell_event({"kind": "command_started", "command": "false"})
    observation.shell_event({"kind": "command_returned", "result": {"success": False}})
    observation.shell_event({"kind": "command_started", "command": "python"})
    assert observation.latest_command["status"] == "executing"
    assert "result" not in observation.latest_command
