"""execute_terminal 的前后台执行：前台结论、后台占位、用户转到后台、停止命令。

命令走真实的 ``LocalExecutionBackend``（经 Terminal Worker 跑 POSIX ``sleep`` / ``echo``），后台回投替换为记录调用，
不写 SQLite。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.core.tool.schemas import ToolExecutionContext
from app.tools.terminal_runtime.command.command_output import CommandOutput as _CommandOutput
from app.tools.terminal_runtime.command.local_backend import LocalExecutionBackend
from app.tools.terminal_runtime.permissions.policy import TerminalExecutionPolicy
from app.tools.tool_handler.terminal.command_results import CommandResultRegistry, command_results
from app.tools.tool_handler.terminal.execute_terminal import ExecuteTerminalTool
from app.tools.tool_handler.write_file import WriteFileTool
from tests.file_knowledge_support import with_read_files
from tests.live_cold_support import registered
from tests.terminal_test_support import requires_terminal_worker

pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="uses POSIX sleep/echo"),
    requires_terminal_worker,
]

_SESSION_ID = 31


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str, dict[str, Any]]]:
    calls: list[tuple[str | None, str, dict[str, Any]]] = []

    def deliver(session_id, tool_call_id, conclusion, *, starts_run=True) -> bool:
        calls.append(
            (
                tool_call_id,
                "execute_terminal",
                {**conclusion.result, "card": conclusion.display_data},
            )
        )
        return True

    monkeypatch.setattr(CommandResultRegistry, "_enqueue_result", staticmethod(deliver))
    yield calls
    command_results.drop_session(_SESSION_ID)


def _context(tmp_path: Path, tool_call_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        step_id="step-1",
        session_id=_SESSION_ID,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=1,
        tool_call_id=tool_call_id,
        terminal_permission_mode="full_access",
    )


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def test_foreground_command_returns_its_result_without_registering(tmp_path, delivered) -> None:
    observation = ExecuteTerminalTool().execute(
        command="echo hello", execution_context=_context(tmp_path, "call-fg")
    )
    assert observation.status == "success" and not observation.background
    assert observation.content.startswith("[exit_code=0]") and "hello" in observation.content
    assert not registered(command_results, _SESSION_ID, "call-fg")
    assert delivered == []


def test_background_command_returns_a_placeholder_then_delivers(tmp_path, delivered) -> None:
    observation = ExecuteTerminalTool().execute(
        command="sleep 0.2; echo done",
        intent="test",
        background=True,
        execution_context=_context(tmp_path, "call-bg"),
    )
    # Agent 主动选后台：先做别的，不挂起。
    assert observation.background and not observation.suspend
    assert observation.background.label == "test"
    assert observation.background.ended_at is None
    assert observation.display_data == {
        "kind": "terminal-result",
        "command": "sleep 0.2; echo done",
        "workdir": str(tmp_path.resolve()),
    }
    _wait_for(lambda: len(delivered) == 1)
    tool_call_id, tool_name, payload = delivered[0]
    assert (tool_call_id, tool_name) == ("call-bg", "execute_terminal")
    assert payload["end_reason"] == "exited" and payload["exit_code"] == 0
    assert "done" in str(payload["output"])
    # 占位卡片的终态展示随结论走：完整输出与退出码，不经模型的输出预算。
    card = payload["card"]
    assert card["kind"] == "terminal-result" and card["exit_code"] == 0
    assert card["command"] == "sleep 0.2; echo done" and "done" in card["output"]


def test_background_label_falls_back_to_first_command_line_without_intent(
    tmp_path, delivered
) -> None:
    """模型漏写 intent 时命令照常进后台，看板标签退回命令首个非空行。"""
    context = _context(tmp_path, "call-bg-no-intent")
    observation = ExecuteTerminalTool().execute(
        command="\n  sleep 0.1; echo done  \necho more", background=True, execution_context=context
    )
    assert observation.background.label == "sleep 0.1; echo done"
    _wait_for(lambda: len(delivered) == 1)


def test_detached_foreground_command_keeps_running_without_suspending(tmp_path, delivered) -> None:
    """前台没有时限：只有用户转走才离开前台，转走后照样回投结论、不挂起 Agent。"""
    import threading

    box: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: box.setdefault(
            "observation",
            ExecuteTerminalTool().execute(
                command="sleep 0.5; echo late",
                execution_context=_context(tmp_path, "call-detach"),
            ),
        )
    )
    thread.start()
    _wait_for(lambda: registered(command_results, _SESSION_ID, "call-detach"))
    assert command_results.detach(_SESSION_ID, "call-detach") == "detached"
    thread.join(2)
    observation = box["observation"]
    assert observation.background and not observation.suspend
    assert "<background_result>" in observation.content
    _wait_for(lambda: len(delivered) == 1)
    assert "late" in str(delivered[0][2]["output"])


def test_cancelling_a_background_command_kills_its_process_tree(tmp_path, delivered) -> None:
    marker = tmp_path / "finished"
    ExecuteTerminalTool().execute(
        command=f"sleep 2; touch {marker}",
        background=True,
        execution_context=_context(tmp_path, "call-cancel"),
    )
    assert "call-cancel" in command_results.live_background(_SESSION_ID)
    command_results.withdraw_calls(_SESSION_ID, ["call-cancel"])
    assert command_results.live_background(_SESSION_ID) == {}
    time.sleep(2.5)
    # 进程树被杀：命令没有跑完；撤销后的结论被登记表丢弃，不回投。
    assert not marker.exists()
    assert delivered == []


def test_background_payload_fits_the_tools_own_budget(tmp_path, delivered, monkeypatch) -> None:
    """后台结论通道没有执行器的通用预算：正文由工具裁一次，标记指向命令自己的完整日志。"""
    from app.config.constant import Constant

    monkeypatch.setattr(Constant.Tools, "MAX_OUTPUT_CHARS", 1000)
    ExecuteTerminalTool().execute(
        command="seq 1 2000",
        background=True,
        execution_context=_context(tmp_path, "call-big"),
    )
    _wait_for(lambda: len(delivered) == 1)
    output = str(delivered[0][2]["output"])
    [log] = list((tmp_path / ".cosir" / "tool-artifacts").iterdir())
    assert len(output) <= 1000 and "2000" in output
    assert output.count("[output truncated:") == 1
    assert log.relative_to(tmp_path).as_posix() in output


def test_local_backend_kills_the_command_when_stopped(tmp_path) -> None:
    marker = tmp_path / "finished"
    backend = LocalExecutionBackend()
    threading.Timer(0.2, backend.stop).start()
    started = time.monotonic()
    output = _CommandOutput(workspace_root=tmp_path, model_budget_chars=20_000)
    result = backend.execute(
        f"sleep 2; touch {marker}",
        str(tmp_path),
        30,
        output,
        policy=TerminalExecutionPolicy(full_access=True, network=True),
    )
    assert result.end == "cancelled" and result.exit_code is None
    assert time.monotonic() - started < 1.5
    time.sleep(2)
    assert not marker.exists()


def test_parent_stop_retains_real_output_without_delivery(tmp_path, delivered) -> None:
    """停止先结束进程，最终输出写回原卡；不把取消结论回投模型。"""
    from app.core.workflow.cancel.run_cancellation_registry import (
        cancellation_registry,
    )

    marker = tmp_path / "ready"
    ExecuteTerminalTool().execute(
        command=f"printf '已收集\\n'; touch '{marker}'; sleep 10",
        background=True,
        execution_context=_context(tmp_path, "call-parent-stop"),
    )
    _wait_for(marker.exists)
    cancellation_registry.request_cancel(_SESSION_ID, "user")
    try:
        displays = command_results.recall_session(_SESSION_ID)
    finally:
        cancellation_registry.consume(_SESSION_ID)
    assert "已收集" in displays["call-parent-stop"]["output"]
    assert delivered == []


def test_timeout_retains_real_output(tmp_path) -> None:
    result = ExecuteTerminalTool().execute(
        command="printf 'before-timeout\\n'; sleep 10",
        timeout=0.2,
        execution_context=_context(tmp_path, "call-timeout-output"),
    )
    assert result.status == "error"
    assert result.display_data["end"] == "timed_out" and "exit_code" not in result.display_data
    assert "before-timeout" in result.display_data["output"]


def test_foreground_cancel_retains_real_output(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    marker = tmp_path / "ready"
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(
            ExecuteTerminalTool().execute,
            command=f"printf 'before-cancel\\n'; touch '{marker}'; sleep 10",
            execution_context=_context(tmp_path, "call-cancel-output"),
        )
        _wait_for(marker.exists)
        assert command_results.request_cancel(_SESSION_ID, "call-cancel-output", driving=False)
        observation = result.result(timeout=5)
    assert observation.status == "cancelled"
    assert "before-cancel" in observation.display_data["output"]
    assert observation.facts.terminal_file_audit.changes[0].operation == "created"


def test_terminal_reads_unlock_literal_writes_only_after_delivery(tmp_path) -> None:
    source = tmp_path / "a file.txt"
    source.write_text("first\nsecond\n", encoding="utf-8")
    context = _context(tmp_path, "read-before-write")
    tool = ExecuteTerminalTool()

    combined = tool.execute(
        command=(
            'target=other.txt; printf other > "$target"; '
            "cat 'a file.txt'; echo replacement > 'a file.txt'"
        ),
        execution_context=context,
    )
    assert combined.display_data == {"status_hint": "需先读取"}
    assert source.read_text() == "first\nsecond\n"
    assert not (tmp_path / "other.txt").exists()

    first_page = tool.execute(command="head -n 1 'a file.txt'", execution_context=context)
    context.file_knowledge.observe(first_page.facts)
    overwrite = tool.execute(command="echo replacement > 'a file.txt'", execution_context=context)
    assert overwrite.display_data == {"status_hint": "需读全文"}

    read = tool.execute(command="cat 'a file.txt'", execution_context=context)
    context.file_knowledge.observe(read.facts)
    overwrite = tool.execute(command="echo replacement > 'a file.txt'", execution_context=context)
    assert overwrite.status == "success"
    [change] = overwrite.facts.terminal_file_audit.changes
    assert (change.path, change.operation, change.recognized, change.previously_read) == (
        "a file.txt",
        "modified",
        True,
        True,
    )
    assert source.read_text() == "replacement\n"
    assert tool.execute(command="rm 'a file.txt'", execution_context=context).display_data == {
        "status_hint": "文件已变化",
    }


def test_unrecognized_write_runs_and_reports_the_actual_change(tmp_path) -> None:
    target = tmp_path / "original.txt"
    target.write_text("before\n", encoding="utf-8")
    context = _context(tmp_path, "complex-write")
    tool = ExecuteTerminalTool()
    command = 'target=original.txt; printf after > "$target"'

    result = tool.execute(command=command, execution_context=context)
    assert result.status == "success"
    assert "unrecognized target, no prior read of the original version" in result.content
    audit = result.facts.terminal_file_audit
    assert audit.complete and audit.total_changes == 1
    [change] = audit.changes
    assert not change.recognized and change.previously_read is False
    assert target.read_text() == "after"


def test_background_reservation_lasts_until_command_settles(tmp_path, delivered) -> None:
    target = tmp_path / "file.txt"
    target.write_text("original", encoding="utf-8")
    command_context = with_read_files(_context(tmp_path, "live-write"), "file.txt")
    edit_context = with_read_files(_context(tmp_path, "concurrent-edit"), "file.txt")
    marker = tmp_path / "ready"
    result = ExecuteTerminalTool().execute(
        command="touch ready; while [ ! -f continue ]; do sleep 0.02; done; echo done > file.txt",
        background=True,
        execution_context=command_context,
    )
    assert result.background
    _wait_for(marker.exists)
    assert WriteFileTool().execute("file.txt", "new", edit_context).display_data == {
        "status_hint": "正在修改"
    }

    (tmp_path / "continue").touch()
    _wait_for(lambda: len(delivered) == 1)
    assert target.read_text() == "done\n"
    assert "modified: 'file.txt' (recognized target)" in str(delivered[0][2]["output"])
    assert WriteFileTool().execute("file.txt", "new", edit_context).display_data == {
        "status_hint": "文件已变化"
    }


def test_background_reports_observed_files_before_completion(
    tmp_path, delivered, monkeypatch
) -> None:
    from app.session_runtime.session_runtime_space_registry import session_runtime_spaces
    from app.tools.terminal_runtime.command import file_audit

    notices = []
    monkeypatch.setattr(file_audit, "_INTERVAL", 0.03)
    monkeypatch.setattr(
        session_runtime_spaces, "enqueue", lambda session, message: notices.append(message)
    )
    result = ExecuteTerminalTool().execute(
        command='target=dynamic.txt; printf data > "$target"; while [ ! -f finish ]; do sleep 0.02; done',
        background=True,
        execution_context=_context(tmp_path, "report-while-running"),
    )
    assert result.background
    _wait_for(lambda: bool(notices))
    notice = notices[0].body
    assert notice.wake_scope() == "none" and not notice.answered_calls()
    assert (
        "report-while-running" in notice.content
        and "'dynamic.txt' (unrecognized target)" in notice.content
    )
    assert delivered == []
    (tmp_path / "finish").touch()
    _wait_for(lambda: bool(delivered))
    assert "created: 'dynamic.txt' (unrecognized target)" in str(delivered[0][2]["output"])
