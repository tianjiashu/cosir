"""终端服务契约：按行读取、条件等待（事件驱动）、输入租约、后台等待、交接与 snapshot 投影。

worker 用假替身按协议喂事件；真实 PTY 行为见 ``test_terminal_shell_integration.py``。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from terminal_test_support import (
    FakeWorker,
    ReadyWorker,
    make_service,
    never,
    run_in_thread,
    start,
    wait_until,
)

from app.config.constant import Constant
from app.core.workflow.deferred import deferred_results as deferred_module
from app.core.workflow.deferred.deferred_results import deferred_results
from app.tools.terminal_runtime.errors import (
    TerminalInputHeldByUserError,
    TerminalIntegrationRequiredError,
    TerminalSessionCapacityError,
    TerminalSessionError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionStateError,
    TerminalWorkerBackpressureError,
    TerminalWorkerUnavailableError,
)
from app.tools.terminal_runtime.terminal_input import TerminalWait


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, dict[str, Any]]]:
    calls: list[tuple[int, str, dict[str, Any]]] = []

    def deliver(
        task_id: int, *, tool_call_id: str | None, tool_name: str, result: dict[str, Any]
    ) -> bool:
        assert tool_call_id  # 终端条目都按触发调用登记
        calls.append((task_id, tool_name, result))
        return True

    monkeypatch.setattr(deferred_module, "deliver_background_result", deliver)
    return calls


@pytest.fixture(autouse=True)
def _drop_entries() -> Any:
    yield
    for task_id in range(1, 10):
        deferred_results.drop_task(task_id)


def write(service: Any, session_id: str, input: dict[str, Any], wait: TerminalWait | None = None,
          *, op: str | None = None, cancelled: Any = never) -> Any:
    return service.write(
        session_id,
        task_id=1,
        operation_id=op or f"op-{time.monotonic_ns()}",
        input=input,
        wait=wait,
        run_id=1,
        tool_call_id=_call_id(),
        is_cancelled=cancelled,
    )


def read(service: Any, session_id: str, **kwargs: Any) -> Any:
    return service.read(
        session_id, task_id=1, run_id=1, tool_call_id=_call_id(), is_cancelled=never, **kwargs
    )


def _call_id() -> str:
    return f"call-{time.monotonic_ns()}"


def echo_command(worker: FakeWorker, input: dict[str, Any]) -> None:
    """模拟 zsh：回车后命令开始、输出两行、命令返回、回到提示符。"""
    if input["type"] != "line":
        return
    text = input["text"]
    worker.output(b"out")
    worker.screen([f"$ {text}", ""], cursor=(1, 0))
    worker.shell("command_started", command=text)
    worker.screen([f"$ {text}", "out1", "out2", ""], cursor=(3, 0))
    worker.shell("command_returned", result={"success": True, "code": 0, "code_kind": "x"})
    worker.screen([f"$ {text}", "out1", "out2", "$ "], cursor=(3, 2))
    worker.shell("shell_prompt", cwd="/w")


# ---------------------------------------------------------------- 生命周期与归属


def test_start_list_close_and_ownership(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    [meta] = service.list_sessions(task_id=1)
    assert meta["session_id"] == session_id
    assert meta["created_by"] == "agent"
    assert "ctrl_c" in meta["keys"] and "interrupt" in meta["signals"]
    with pytest.raises(TerminalSessionOwnershipError):
        read(service, session_id) if False else service.read(
            session_id, task_id=2, run_id=1, tool_call_id=_call_id(), is_cancelled=never
        )
    with pytest.raises(TerminalSessionNotFoundError):
        service.close("missing", task_id=1)
    closed = service.close(session_id, task_id=1)
    assert closed["status"] == "closed"
    assert workers[0].closed
    assert service.list_sessions(task_id=1) == []
    assert service.list_sessions(task_id=1, include_ended=True)[0]["status"] == "closed"
    assert service.active_session_count() == 0


def test_agent_per_task_limit_leaves_room_for_user(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Constant.Terminal, "MAX_AGENT_SESSIONS_PER_TASK", 2)
    service, _ = make_service(monkeypatch)
    start(service, tmp_path)
    start(service, tmp_path)
    with pytest.raises(TerminalSessionCapacityError):
        start(service, tmp_path)
    start(service, tmp_path, created_by="user")
    start(service, tmp_path, task_id=2)
    assert service.active_session_count() == 4


def test_worker_exit_cleanup_runs_off_reader_thread_and_notifies(monkeypatch, tmp_path) -> None:
    changes: list[int] = []
    service, workers = make_service(monkeypatch, on_changed=changes.append)
    session_id = start(service, tmp_path)
    workers[0].exit(3)
    wait_until(lambda: workers[0].closed)
    [entry] = service.project_task_sessions(1)
    assert entry["status"] == "exited" and entry["exitCode"] == 3
    wait_until(lambda: len(changes) >= 2)
    assert set(changes) == {1}
    assert read(service, session_id).status == "exited"


def test_worker_disconnect_fails_session_but_not_after_exit(monkeypatch, tmp_path) -> None:
    """读线程补发的断开错误把活会话收敛为 failed；shell 已退出后到达的断开不改写终态。"""
    service, workers = make_service(monkeypatch)
    start(service, tmp_path)
    workers[0].emit({"type": "error", "code": "WORKER_DISCONNECTED"})
    wait_until(lambda: workers[0].closed)
    [entry] = service.project_task_sessions(1)
    assert (entry["status"], entry["endReason"]) == ("failed", "worker_disconnected")

    exited = start(service, tmp_path)
    workers[1].exit(0)
    workers[1].emit({"type": "error", "code": "WORKER_DISCONNECTED"})
    wait_until(lambda: workers[1].closed)
    entry = next(e for e in service.project_task_sessions(1) if e["sessionId"] == exited)
    assert (entry["status"], entry["exitCode"]) == ("exited", 0)


def test_worker_unavailable_on_commands_does_not_end_session(monkeypatch, tmp_path) -> None:
    """信号回执超时、按键时 worker 不可用只报错给调用方，会话不被判为 failed。"""
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)

    def slow_ack(signal_name: str) -> None:
        raise TerminalWorkerUnavailableError("terminal worker signal acknowledgement timed out")

    def unavailable(input: dict[str, Any]) -> None:
        raise TerminalWorkerUnavailableError("terminal worker is not ready")

    workers[0].signal = slow_ack  # type: ignore[method-assign]
    fast = TerminalWait(until="elapsed", timeout_ms=0)
    with pytest.raises(TerminalWorkerUnavailableError):
        write(service, session_id, {"type": "signal", "signal": "interrupt"}, fast)
    workers[0].write = unavailable  # type: ignore[method-assign]
    with pytest.raises(TerminalWorkerUnavailableError):
        service.user_input(session_id, task_id=1, data=b"a")
    assert read(service, session_id).status == "running"
    assert not workers[0].closed


def test_user_input_after_shell_exit_keeps_exited(monkeypatch, tmp_path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].exit(0)
    wait_until(lambda: workers[0].closed)
    assert service.user_input(session_id, task_id=1, data=b"a") == "session_ended"
    assert read(service, session_id).status == "exited"


def test_project_task_sessions_orders_active_first_and_keeps_ten_ended(monkeypatch, tmp_path):
    service, _ = make_service(monkeypatch)
    ended = [start(service, tmp_path, created_by="user") for _ in range(12)]
    for session_id in ended:
        service.close(session_id, task_id=1)
    live = start(service, tmp_path)
    entries = service.project_task_sessions(1)
    assert entries[0]["sessionId"] == live
    assert len(entries) == 11
    assert [e["sessionId"] for e in entries[1:]] == ended[2:]
    assert set(entries[0]) == {
        "sessionId", "label", "shellKind", "createdBy", "status", "endReason", "exitCode",
        "integration", "inputHolder", "commandRunning", "handoff", "createdAt",
    }
    assert entries[0]["integration"] == "unavailable"


# ---------------------------------------------------------------- 读取


def test_read_tail_range_and_no_change_notice(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].screen(["$ "], cursor=(0, 2), history=[f"h{i}\n" for i in range(1, 101)])
    result = read(service, session_id)
    assert result.lines == (62, 101)
    assert "62│h62" in result.text and "lines 62-101 of 1-101 (live from 101)" in result.text
    ranged = read(service, session_id, start_line=10, limit=3)
    assert ranged.lines == (10, 12) and "10│h10\n11│h11\n12│h12" in ranged.text
    read(service, session_id, tail=5)
    again = read(service, session_id, tail=5)
    assert "no change since your last read" in again.text
    # 只读历史区间不计重复。
    read(service, session_id, start_line=1, limit=2)
    assert "1│h1" in read(service, session_id, start_line=1, limit=2).text


def test_user_typed_lines_are_reported_once(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path, created_by="user")
    workers[0].screen(["a", "b", "$ "], cursor=(2, 2))
    assert service.user_input(session_id, task_id=1, data=b"l") == "accepted"
    assert workers[0].writes[-1] == {"type": "raw", "data_base64": "bA=="}
    assert "user typed lines 3-3" in read(service, session_id).text
    assert "user typed" not in read(service, session_id, tail=3).text


# ---------------------------------------------------------------- 条件等待


def test_write_command_returned_reports_output_range(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    workers[0].on_write = echo_command
    workers[0].screen(["$ "], cursor=(0, 2))
    result = write(service, session_id, {"type": "line", "text": "ls"},
                   TerminalWait(until="command_returned"))
    assert result.wait_result == {"until": "command_returned", "end_reason": "command_returned"}
    assert "cmd 1 `ls` returned exit 0 · output lines 2-3" in result.text
    assert "1│$ ls\n2│out1\n3│out2" in result.text
    assert result.input_summary == "ls" and result.input_type == "line"


def test_command_returned_without_integration_is_rejected(monkeypatch, tmp_path: Path) -> None:
    service, _ = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    with pytest.raises(TerminalIntegrationRequiredError):
        write(service, session_id, {"type": "line", "text": "ls"},
              TerminalWait(until="command_returned"))


def test_match_wakes_on_event_without_polling(monkeypatch, tmp_path: Path) -> None:
    # 时间片调到 5 秒：若靠轮询，等待不可能在事件到达后立刻结束。
    monkeypatch.setattr(Constant.Terminal, "WAIT_CANCEL_CHECK_SECONDS", 5.0)
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    wait = TerminalWait(until="match", pattern=r"Listening on \d+", timeout_ms=20000)
    thread, box = run_in_thread(lambda: read(service, session_id, wait=wait))
    time.sleep(0.1)
    began = time.monotonic()
    workers[0].screen(["$ serve", "Listening on 8080"], cursor=(1, 17))
    thread.join(2)
    assert time.monotonic() - began < 1.0
    assert box["result"].wait_result == {
        "until": "match", "end_reason": "matched", "matched_line": 2
    }


def _foreground_read(service: Any, session_id: str, tool_call_id: str):
    wait = TerminalWait(until="match", pattern=r"DONE", timeout_ms=20000)
    thread, box = run_in_thread(
        lambda: service.read(
            session_id,
            task_id=1,
            run_id=1,
            tool_call_id=tool_call_id,
            is_cancelled=never,
            wait=wait,
        )
    )
    wait_until(lambda: deferred_results.find(1, tool_call_id) is not None)
    return thread, box


def test_foreground_wait_is_registered_and_settles_without_delivery(
    monkeypatch, tmp_path, delivered
) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    thread, box = _foreground_read(service, session_id, "call-fg")
    assert deferred_results.find(1, "call-fg").mode == "foreground"
    workers[0].screen(["$ build", "DONE"], cursor=(1, 4))
    thread.join(2)
    result = box["result"]
    assert result.wait_result["end_reason"] == "matched" and not result.deferred
    assert deferred_results.find(1, "call-fg") is None
    assert delivered == []


def test_detached_foreground_wait_returns_placeholder_then_delivers(
    monkeypatch, tmp_path, delivered
) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    thread, box = _foreground_read(service, session_id, "call-fg")
    assert deferred_results.detach("call-fg") == "detached"
    thread.join(2)
    placeholder = box["result"]
    assert placeholder.deferred and not placeholder.suspended
    assert placeholder.wait_result == {"until": "match", "end_reason": "background"}
    assert deferred_results.find(1, "call-fg").mode == "background"
    # 后台线程接着等同一个条件，满足后按后台回投。
    workers[0].screen(["$ build", "DONE"], cursor=(1, 4))
    wait_until(lambda: len(delivered) == 1)
    task_id, tool_name, payload = delivered[0]
    assert (task_id, tool_name) == (1, "terminal_wait")
    assert payload["wait"]["end_reason"] == "matched"


def test_user_cancel_ends_a_foreground_wait_as_cancelled(monkeypatch, tmp_path, delivered) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    thread, box = _foreground_read(service, session_id, "call-fg")
    dropped = deferred_results.cancel("call-fg")
    assert dropped is not None and dropped.mode == "foreground"
    thread.join(2)
    assert box["result"].wait_result["end_reason"] == "cancelled"
    assert delivered == []


def test_background_wait_and_handoff_placeholders_are_deferred(monkeypatch, tmp_path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path, created_by="user")
    long_read = service.read(
        session_id, task_id=1, run_id=1, tool_call_id="call-long", is_cancelled=never,
        wait=TerminalWait(until="match", pattern="DONE", timeout_ms=600_000),
    )
    handoff = service.write(
        session_id, task_id=1, operation_id="op-h", run_id=1, tool_call_id="call-h",
        is_cancelled=never, wait=None, input={"type": "handoff", "reason": "password"},
    )
    assert long_read.deferred and long_read.suspended
    assert handoff.deferred and handoff.suspended


def test_output_quiet_elapsed_and_timeout(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    began = time.monotonic()
    quiet = write(service, session_id, {"type": "line", "text": "x"},
                  TerminalWait(until="output_quiet", quiet_ms=200, timeout_ms=3000))
    assert quiet.wait_result["end_reason"] == "output_quiet"
    assert 0.18 < time.monotonic() - began < 1.0
    elapsed = read(service, session_id, wait=TerminalWait(until="elapsed", timeout_ms=150))
    assert elapsed.wait_result["end_reason"] == "elapsed"
    timeout = read(service, session_id,
                   wait=TerminalWait(until="match", pattern="never", timeout_ms=150))
    assert timeout.wait_result["end_reason"] == "timeout"


def test_cancel_and_session_end_wake_waits(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    flag = {"cancel": False}
    thread, box = run_in_thread(lambda: service.read(
        session_id, task_id=1, run_id=1, tool_call_id=_call_id(),
        is_cancelled=lambda: flag["cancel"],
        wait=TerminalWait(until="elapsed", timeout_ms=20000)))
    time.sleep(0.05)
    flag["cancel"] = True
    thread.join(1)
    assert box["result"].wait_result["end_reason"] == "cancelled"
    thread, box = run_in_thread(lambda: read(
        service, session_id, wait=TerminalWait(until="elapsed", timeout_ms=20000)))
    time.sleep(0.05)
    workers[0].exit(0)
    thread.join(1)
    assert box["result"].wait_result["end_reason"] == "session_ended"


def test_read_command_returned_waits_for_running_command(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    idle = read(service, session_id, wait=TerminalWait(until="command_returned"))
    assert idle.wait_result["end_reason"] == "idle"
    workers[0].shell("command_started", command="sleep 1")
    thread, box = run_in_thread(lambda: read(
        service, session_id, wait=TerminalWait(until="command_returned", timeout_ms=5000)))
    time.sleep(0.05)
    workers[0].shell("command_returned", result={"success": False, "code": 2})
    thread.join(1)
    assert box["result"].wait_result["end_reason"] == "command_returned"
    assert "exit 2" in box["result"].text


def test_keys_signal_and_duplicate_operation(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    fast = TerminalWait(until="elapsed", timeout_ms=0)
    write(service, session_id, {"type": "keys", "keys": ["down", "enter"]}, fast, op="k")
    assert workers[0].writes == [{"type": "keys", "keys": ["down", "enter"]}]
    again = write(service, session_id, {"type": "keys", "keys": ["down", "enter"]}, fast, op="k")
    assert "duplicate operation_id" in again.text and len(workers[0].writes) == 1
    with pytest.raises(TerminalSessionError):
        write(service, session_id, {"type": "line", "text": "y"}, fast, op="k")
    write(service, session_id, {"type": "signal", "signal": "interrupt"}, fast)
    assert workers[0].signals == ["interrupt"]


# ---------------------------------------------------------------- 输入租约


def test_agent_waits_for_user_lease_then_proceeds(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Constant.Terminal, "USER_LEASE_IDLE_SECONDS", 30.0)
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    service.user_input(session_id, task_id=1, data=b"a")
    assert service.project_task_sessions(1)[0]["inputHolder"] == "user"
    thread, box = run_in_thread(lambda: write(
        service, session_id, {"type": "line", "text": "ls"}, TerminalWait(until="elapsed",
                                                                         timeout_ms=0)))
    time.sleep(0.2)
    assert len(workers[0].writes) == 1
    service.release_user_input(session_id, task_id=1)
    thread.join(2)
    assert "result" in box and workers[0].writes[-1]["type"] == "line"
    assert service.project_task_sessions(1)[0]["inputHolder"] is None


def test_agent_times_out_with_retryable_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Constant.Terminal, "USER_LEASE_IDLE_SECONDS", 30.0)
    monkeypatch.setattr(Constant.Terminal, "AGENT_LEASE_WAIT_SECONDS", 0.2)
    service, _ = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    service.user_input(session_id, task_id=1, data=b"a")
    with pytest.raises(TerminalInputHeldByUserError) as caught:
        write(service, session_id, {"type": "line", "text": "ls"})
    assert caught.value.retryable


def test_user_rejected_while_agent_types_except_ctrl_c(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    thread, _ = run_in_thread(lambda: write(
        service, session_id, {"type": "line", "text": "sleep"},
        TerminalWait(until="elapsed", timeout_ms=400)))
    wait_until(lambda: service.project_task_sessions(1)[0]["inputHolder"] == "agent")
    assert service.user_input(session_id, task_id=1, data=b"q") == "rejected_agent_input"
    assert service.user_input(session_id, task_id=1, data=b"\x03") == "accepted"
    assert workers[0].writes[-1] == {"type": "raw", "data_base64": "Aw=="}
    assert service.project_task_sessions(1)[0]["inputHolder"] == "user"
    thread.join(2)
    # Agent 收尾不会抢走用户刚取得的租约。
    assert service.project_task_sessions(1)[0]["inputHolder"] == "user"


def test_user_lease_idle_release_and_executing_hold(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Constant.Terminal, "USER_LEASE_IDLE_SECONDS", 0.15)
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    service.user_input(session_id, task_id=1, data=b"a")
    wait_until(lambda: service.project_task_sessions(1)[0]["inputHolder"] is None)
    service.user_input(session_id, task_id=1, data=b"\r")
    workers[0].shell("command_started", command="vim")
    time.sleep(0.4)
    assert service.project_task_sessions(1)[0]["inputHolder"] == "user"
    workers[0].shell("command_returned", result={"success": True, "code": 0})
    wait_until(lambda: service.project_task_sessions(1)[0]["inputHolder"] is None)
    assert service.user_input(session_id, task_id=1, data=b"x") == "accepted"
    service.close(session_id, task_id=1)
    assert service.user_input(session_id, task_id=1, data=b"x") == "session_ended"


def test_resize_is_forwarded_and_geometry_follows_screen(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    service.resize(session_id, task_id=1, rows=40, cols=120)
    assert workers[0].resizes == [(40, 120)]
    workers[0].screen(["$ "], size=(40, 120))
    assert service.list_sessions(task_id=1)[0]["geometry"] == {"rows": 40, "columns": 120}


# ---------------------------------------------------------------- 后台等待与交接


def test_long_wait_suspends_then_delivers_background_result(monkeypatch, tmp_path, delivered):
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    result = write(service, session_id, {"type": "line", "text": "make"},
                   TerminalWait(until="match", pattern="BUILD OK", timeout_ms=600_000))
    assert result.suspended
    assert "<background_result>" in result.text
    assert deferred_results.pending(1) == [f"terminal_wait:{session_id}"]
    assert service.project_task_sessions(1)[0]["inputHolder"] is None
    workers[0].screen(["$ make", "BUILD OK"], cursor=(1, 8))
    wait_until(lambda: len(delivered) == 1)
    task_id, tool_name, payload = delivered[0]
    assert (task_id, tool_name) == (1, "terminal_wait")
    assert payload["wait"] == {"until": "match", "end_reason": "matched", "matched_line": 2}
    assert "2│BUILD OK" in payload["tail"]
    assert deferred_results.pending(1) == []


def test_background_wait_and_handoff_are_keyed_by_the_tool_call(monkeypatch, tmp_path) -> None:
    """后台等待与交接以触发它们的工具调用登记，结论带着同一个 ``tool_call_id`` 回投。"""
    calls: list[tuple[str | None, str]] = []

    def deliver(task_id, *, tool_call_id, tool_name, result) -> bool:
        calls.append((tool_call_id, tool_name))
        return True

    monkeypatch.setattr(deferred_module, "deliver_background_result", deliver)
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path, created_by="user")
    service.read(
        session_id, task_id=1, run_id=1, tool_call_id="call-long-read", is_cancelled=never,
        wait=TerminalWait(until="match", pattern="DONE", timeout_ms=600_000),
    )
    service.write(
        session_id, task_id=1, operation_id="op-handoff", run_id=1,
        tool_call_id="call-handoff", is_cancelled=never, wait=None,
        input={"type": "handoff", "reason": "enter the password"},
    )
    assert set(deferred_results._entries) == {"call-long-read", "call-handoff"}
    workers[0].screen(["$ ", "DONE"], cursor=(1, 4))
    service.complete_handoff(session_id, task_id=1)
    wait_until(lambda: len(calls) == 2)
    assert sorted(calls) == [
        ("call-handoff", "terminal_handoff"),
        ("call-long-read", "terminal_wait"),
    ]


def test_run_cancel_drops_background_wait_without_delivery(monkeypatch, tmp_path, delivered):
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    read(service, session_id, wait=TerminalWait(until="elapsed", timeout_ms=60_000))
    deferred_results.drop_task(1)
    time.sleep(0.3)
    workers[0].screen(["x"])
    time.sleep(0.1)
    assert delivered == []


def test_handoff_flow_complete_and_cancel(monkeypatch, tmp_path, delivered):
    changes: list[int] = []
    service, workers = make_service(monkeypatch, on_changed=changes.append)
    session_id = start(service, tmp_path)
    result = write(service, session_id, {"type": "handoff", "reason": "Enter sudo password"})
    assert result.suspended and result.input_type == "handoff"
    assert service.project_task_sessions(1)[0]["handoff"] == {"reason": "Enter sudo password"}
    service.complete_handoff(session_id, task_id=1)
    wait_until(lambda: len(delivered) == 1)
    assert delivered[0][1] == "terminal_handoff"
    assert delivered[0][2]["wait"] == {"end_reason": "user_completed"}
    assert service.project_task_sessions(1)[0]["handoff"] is None
    # Run 取消撤销交接：清除状态、不投递。
    write(service, session_id, {"type": "handoff", "reason": "confirm"})
    deferred_results.drop_task(1)
    assert service.project_task_sessions(1)[0]["handoff"] is None
    service.complete_handoff(session_id, task_id=1)
    time.sleep(0.1)
    assert len(delivered) == 1


def test_user_lease_release_does_not_end_handoff(monkeypatch, tmp_path, delivered):
    """输密码时停顿会让租约空闲释放；交接只认「交还」，不能因此唤醒 Agent。"""
    monkeypatch.setattr(Constant.Terminal, "USER_LEASE_IDLE_SECONDS", 0.1)
    service, _ = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    write(service, session_id, {"type": "handoff", "reason": "password"})
    service.user_input(session_id, task_id=1, data=b"secr")
    wait_until(lambda: service.project_task_sessions(1)[0]["inputHolder"] is None)
    service.user_input(session_id, task_id=1, data=b"et")
    service.release_user_input(session_id, task_id=1)
    time.sleep(0.2)
    assert delivered == []
    assert service.project_task_sessions(1)[0]["handoff"] == {"reason": "password"}
    service.complete_handoff(session_id, task_id=1)
    wait_until(lambda: len(delivered) == 1)
    assert delivered[0][2]["wait"] == {"end_reason": "user_completed"}
    assert delivered[0][2]["handoff_reason"] == "password"


def test_session_end_delivers_handoff(monkeypatch, tmp_path, delivered):
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    write(service, session_id, {"type": "handoff", "reason": "password"})
    workers[0].exit(1)
    wait_until(lambda: len(delivered) == 1)
    assert delivered[0][2]["wait"] == {"end_reason": "session_ended"}


def test_shutdown_after_watches_close_writes_nothing(monkeypatch, tmp_path, delivered):
    """后端关闭：lifespan 先关闭登记表，结束会话唤醒的后台等待与交接都不再投递。"""
    service, _ = make_service(monkeypatch)
    waiting = start(service, tmp_path)
    handed = start(service, tmp_path)
    read(service, waiting, wait=TerminalWait(until="elapsed", timeout_ms=600_000))
    write(service, handed, {"type": "handoff", "reason": "password"})
    assert len(deferred_results.pending(1)) == 2
    deferred_results.close()
    assert deferred_results.pending(1) == []
    service.shutdown()
    time.sleep(0.2)
    assert delivered == []


def test_read_command_returned_on_idle_shell_returns_without_suspending(monkeypatch, tmp_path):
    service, _ = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    result = read(
        service, session_id, wait=TerminalWait(until="command_returned", timeout_ms=600_000)
    )
    assert not result.suspended
    assert result.wait_result == {"until": "command_returned", "end_reason": "idle"}
    assert deferred_results.pending(1) == []


def test_match_after_line_input_skips_the_echo_line(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    command = "make && echo BUILD_DONE"

    def echo_then_finish(worker: FakeWorker, input: dict[str, Any]) -> None:
        worker.screen([f"$ {command}", ""], cursor=(1, 0))

    workers[0].on_write = echo_then_finish
    thread, box = run_in_thread(lambda: write(
        service, session_id, {"type": "line", "text": command},
        TerminalWait(until="match", pattern="BUILD_DONE", timeout_ms=2_000),
    ))
    time.sleep(0.2)
    assert thread.is_alive(), "回显行不应命中 match"
    workers[0].screen([f"$ {command}", "BUILD_DONE", "$ "], cursor=(2, 2))
    thread.join(2)
    assert box["result"].wait_result == {
        "until": "match", "end_reason": "matched", "matched_line": 2
    }


def test_match_on_read_includes_the_cursor_line(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].screen(["Password:"], cursor=(0, 9))
    result = read(service, session_id, wait=TerminalWait(until="match", pattern="Password"))
    assert result.wait_result == {"until": "match", "end_reason": "matched", "matched_line": 1}


class SlowStartWorker(FakeWorker):
    """握手阻塞到测试放行，用来验证握手期间不持会话锁。"""

    release = threading.Event()
    entered = threading.Event()

    def start(self, spec: Any, cwd: str, on_event: Any) -> None:
        type(self).entered.set()
        assert type(self).release.wait(5)
        super().start(spec, cwd, on_event)


@pytest.fixture
def slow_start() -> Any:
    SlowStartWorker.release = threading.Event()
    SlowStartWorker.entered = threading.Event()
    yield SlowStartWorker
    SlowStartWorker.release.set()


def test_handshake_does_not_block_projection_or_input(monkeypatch, tmp_path, slow_start) -> None:
    service, workers = make_service(monkeypatch, worker_type=slow_start)
    thread, box = run_in_thread(lambda: start(service, tmp_path, created_by="user"))
    assert slow_start.entered.wait(2)
    session_id = str(service.project_task_sessions(1)[0]["sessionId"])
    began = time.monotonic()
    [entry] = service.project_task_sessions(1)
    assert entry["status"] == "starting"
    assert service.list_sessions(task_id=1)[0]["status"] == "starting"
    with pytest.raises(TerminalSessionStateError):
        service.user_input(session_id, task_id=1, data=b"x")
    service.resize(session_id, task_id=1, rows=30, cols=100)
    assert time.monotonic() - began < 1
    assert workers[0].writes == [] and workers[0].resizes == []
    slow_start.release.set()
    thread.join(2)
    assert box["result"] == session_id
    assert service.project_task_sessions(1)[0]["status"] == "running"
    # 启动期间记下的尺寸在握手完成后补发。
    assert workers[0].resizes == [(30, 100)]


def test_close_during_handshake_disposes_after_start_returns(
    monkeypatch, tmp_path, slow_start
) -> None:
    service, workers = make_service(monkeypatch, worker_type=slow_start)
    thread, box = run_in_thread(lambda: start(service, tmp_path, created_by="user"))
    assert slow_start.entered.wait(2)
    session_id = str(service.project_task_sessions(1)[0]["sessionId"])
    closer, closed = run_in_thread(lambda: service.close(session_id, task_id=1))
    time.sleep(0.2)
    # 终态已迁移，但 worker 要等握手结束才关闭，不在 start 半途被关。
    assert service.project_task_sessions(1)[0]["status"] == "closed"
    assert not workers[0].closed and closer.is_alive()
    slow_start.release.set()
    thread.join(2)
    closer.join(2)
    assert workers[0].closed
    assert closed["result"]["status"] == "closed"
    assert box["result"] == session_id


def test_user_input_backpressure_leaves_lease_and_marks_unchanged(monkeypatch, tmp_path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)

    def full(worker: FakeWorker, input: dict[str, Any]) -> None:
        raise TerminalWorkerBackpressureError("terminal worker input queue is full")

    workers[0].on_write = full
    with pytest.raises(TerminalWorkerBackpressureError):
        service.user_input(session_id, task_id=1, data=b"a")
    assert service.project_task_sessions(1)[0]["inputHolder"] is None
    assert "user typed" not in read(service, session_id).text
