"""终端服务契约：按行读取、条件等待（事件驱动）、输入仲裁、后台等待、交接与 snapshot 投影。

worker 用假替身按协议喂事件；真实 PTY 行为见 ``test_terminal_shell_integration.py``。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from terminal_test_support import (
    session_entries,
    FakeWorker,
    ReadyWorker,
    active_session_ids,
    make_service,
    never,
    run_in_thread,
    geometry,
    session_view,
    start,
    wait_until,
)

from app.config.constant import Constant
from app.core.tool.background import deferred_registry as deferred_module
from app.core.tool.background.deferred_registry import deferred_registry
from app.tools.terminal_runtime.errors import (
    TerminalIntegrationRequiredError,
    TerminalSessionCapacityError,
    TerminalSessionError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionStateError,
    TerminalUserCommandRunningError,
    TerminalUserTypingError,
    TerminalWorkerBackpressureError,
    TerminalWorkerUnavailableError,
)
from app.tools.terminal_runtime.inputs import TerminalWait


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
        deferred_registry.drop_task(task_id)


def write(service: Any, session_id: str, input: dict[str, Any], wait: TerminalWait | None = None,
          *, op: str | None = None, cancelled: Any = never) -> Any:
    return service.require(session_id, 1).write(
        input,
        op or f"op-{time.monotonic_ns()}",
        wait,
        run_id=1,
        tool_call_id=_call_id(),
        is_cancelled=cancelled,
    )


def read(service: Any, session_id: str, **kwargs: Any) -> Any:
    return service.require(session_id, 1).read(is_cancelled=never, **kwargs)


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
    meta = session_view(service, session_id)
    assert service.require(session_id, 1).created_by == "agent"
    assert meta.summary.startswith(f"session {session_id} by agent")
    with pytest.raises(TerminalSessionOwnershipError):
        service.require(session_id, 2)
    with pytest.raises(TerminalSessionNotFoundError):
        service.close("missing", task_id=1, by="agent")
    closed = service.close(session_id, task_id=1, by="agent")
    assert closed.status == "closed"
    assert workers[0].closed
    assert active_session_ids(service) == []
    assert session_entries(service, 1)[0]["status"] == "closed"
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
    [entry] = session_entries(service, 1)
    assert entry["status"] == "exited" and entry["exitCode"] == 3
    wait_until(lambda: len(changes) >= 2)
    assert set(changes) == {1}
    assert read(service, session_id).status == "exited"


def test_agent_is_told_when_its_session_ends_unnoticed(monkeypatch, tmp_path) -> None:
    """Agent 不知情的结束（shell 退出、用户关闭）投递一次通知；自己关闭与用户的会话不通知。"""
    notices: list[tuple[int, str]] = []
    service, workers = make_service(
        monkeypatch, on_agent_ended=lambda task, text: notices.append((task, text))
    )
    exited = start(service, tmp_path)
    workers[0].screen(["$ npm run dev", "crashed: port in use", "$ "], cursor=(2, 2))
    workers[0].exit(1)
    wait_until(lambda: len(notices) == 1)
    task_id, text = notices[0]
    assert task_id == 1 and f"session {exited}" in text and "exit 1" in text
    assert "crashed: port in use" in text

    by_user = start(service, tmp_path)
    service.close(by_user, task_id=1, by="user")
    wait_until(lambda: len(notices) == 2)
    assert "closed_by_user" in notices[1][1]

    own = start(service, tmp_path)
    service.close(own, task_id=1, by="agent")
    start(service, tmp_path, created_by="user")
    workers[3].exit(0)
    wait_until(lambda: workers[3].closed)
    service.shutdown()
    assert len(notices) == 2 and own not in "".join(text for _, text in notices)


def test_session_end_during_agent_wait_is_not_announced_twice(monkeypatch, tmp_path) -> None:
    notices: list[tuple[int, str]] = []
    service, workers = make_service(
        monkeypatch, on_agent_ended=lambda task, text: notices.append((task, text))
    )
    session_id = start(service, tmp_path)
    thread, box = run_in_thread(
        lambda: read(service, session_id, wait=TerminalWait(until="elapsed", timeout_ms=5000))
    )
    wait_until(lambda: service._lookup(session_id)._agent_waits == 1)
    workers[0].exit(0)
    thread.join(2)
    assert box["result"].wait_result["end_reason"] == "session_ended"
    service.shutdown()
    assert notices == []


def test_worker_disconnect_fails_session_but_not_after_exit(monkeypatch, tmp_path) -> None:
    """读线程补发的断开错误把活会话收敛为 failed；shell 已退出后到达的断开不改写终态。"""
    service, workers = make_service(monkeypatch)
    start(service, tmp_path)
    workers[0].emit({"type": "error", "code": "WORKER_DISCONNECTED"})
    wait_until(lambda: workers[0].closed)
    [entry] = session_entries(service, 1)
    assert (entry["status"], entry["endReason"]) == ("failed", "worker_disconnected")

    exited = start(service, tmp_path)
    workers[1].exit(0)
    workers[1].emit({"type": "error", "code": "WORKER_DISCONNECTED"})
    wait_until(lambda: workers[1].closed)
    entry = next(e for e in session_entries(service, 1) if e["sessionId"] == exited)
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
        service.require(session_id, 1).user_input(b"a")
    assert read(service, session_id).status == "running"
    assert not workers[0].closed


def test_user_input_after_shell_exit_keeps_exited(monkeypatch, tmp_path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].exit(0)
    wait_until(lambda: workers[0].closed)
    assert service.require(session_id, 1).user_input(b"a") == "session_ended"
    assert read(service, session_id).status == "exited"


def test_task_runtimes_orders_active_first_and_keeps_ten_ended(monkeypatch, tmp_path):
    service, _ = make_service(monkeypatch)
    ended = [start(service, tmp_path, created_by="user") for _ in range(12)]
    for session_id in ended:
        service.close(session_id, task_id=1, by="agent")
    live = start(service, tmp_path)
    entries = session_entries(service, 1)
    assert entries[0]["sessionId"] == live
    assert len(entries) == 11
    assert [e["sessionId"] for e in entries[1:]] == ended[2:]
    assert set(entries[0]) == {
        "sessionId", "label", "shellKind", "createdBy", "status", "endReason", "exitCode",
        "integration", "commandRunning", "handoff",
        "createdAt",
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


def test_read_pattern_returns_only_matching_lines_with_continuation(monkeypatch, tmp_path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    history = [f"{'error' if i % 10 == 0 else 'ok'} {i}\n" for i in range(1, 101)]
    workers[0].screen(["$ "], cursor=(0, 2), history=history)

    found = read(service, session_id, pattern=r"^error")
    assert found.lines == (10, 100) and not found.screen
    assert "10│error 10\n20│error 20" in found.text and "ok 11" not in found.text
    assert "10 lines match /^error/ in lines 1-101" in found.text

    capped = read(service, session_id, pattern=r"^error", limit=3)
    assert capped.lines == (10, 30)
    assert "more matches, continue with start_line=40" in capped.text
    resumed = read(service, session_id, pattern=r"^error", start_line=40, limit=3)
    assert resumed.lines == (40, 60)
    # 同一搜索无变化时只给一行提示。
    assert "no change since your last read" in read(
        service, session_id, pattern=r"^error", start_line=40, limit=3
    ).text


def test_read_pattern_rejects_tail_and_invalid_regex() -> None:
    from pydantic import ValidationError

    from app.tools.tool_models.terminal_session_args import TerminalReadArgs

    with pytest.raises(ValidationError, match="tail"):
        TerminalReadArgs.model_validate({"session_id": "s", "pattern": "x", "tail": 5})
    with pytest.raises(ValidationError, match="invalid pattern"):
        TerminalReadArgs.model_validate({"session_id": "s", "pattern": "("})


def test_user_typed_lines_are_reported_once(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path, created_by="user")
    workers[0].screen(["a", "b", "$ "], cursor=(2, 2))
    assert service.require(session_id, 1).user_input(b"l") == "accepted"
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
    thread, box = run_in_thread(lambda: service.require(session_id, 1).read(
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


# ---------------------------------------------------------------- 输入仲裁

_NO_WAIT = TerminalWait(until="elapsed", timeout_ms=0)


def test_agent_waits_for_user_to_pause_typing_then_proceeds(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Constant.Terminal, "USER_TYPING_IDLE_SECONDS", 0.3)
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    service.require(session_id, 1).user_input(b"a")
    assert "user is typing" in read(service, session_id).text
    thread, box = run_in_thread(
        lambda: write(service, session_id, {"type": "line", "text": "ls"}, _NO_WAIT)
    )
    time.sleep(0.1)
    assert len(workers[0].writes) == 1
    thread.join(2)
    assert "result" in box and workers[0].writes[-1] == {"type": "line", "text": "ls"}


def test_agent_times_out_with_retryable_error_while_user_keeps_typing(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Constant.Terminal, "USER_TYPING_IDLE_SECONDS", 30.0)
    monkeypatch.setattr(Constant.Terminal, "AGENT_TYPING_WAIT_SECONDS", 0.2)
    service, _ = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    service.require(session_id, 1).user_input(b"a")
    with pytest.raises(TerminalUserTypingError) as caught:
        write(service, session_id, {"type": "line", "text": "ls"})
    assert caught.value.retryable


def test_user_input_is_never_rejected_while_agent_waits(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    thread, box = run_in_thread(lambda: write(
        service, session_id, {"type": "line", "text": "sleep"},
        TerminalWait(until="elapsed", timeout_ms=400)))
    wait_until(lambda: workers[0].writes == [{"type": "line", "text": "sleep"}])
    assert service.require(session_id, 1).user_input(b"q") == "accepted"
    assert workers[0].writes[-1] == {"type": "raw", "data_base64": "cQ=="}
    thread.join(2)
    assert "user typed lines" in box["result"].text
    service.close(session_id, task_id=1, by="agent")
    assert service.require(session_id, 1).user_input(b"x") == "session_ended"


def test_agent_rejected_at_once_while_users_command_runs(monkeypatch, tmp_path: Path) -> None:
    """用户启动的命令可能长驻：Agent 输入立即失败、不等待，命令返回后恢复。"""
    monkeypatch.setattr(Constant.Terminal, "USER_TYPING_IDLE_SECONDS", 0.05)
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    service.require(session_id, 1).user_input(b"npm run dev\r")
    workers[0].shell("command_started", command="npm run dev")
    time.sleep(0.1)
    assert "started by user" in read(service, session_id).text
    begin = time.monotonic()
    with pytest.raises(TerminalUserCommandRunningError) as caught:
        write(service, session_id, {"type": "signal", "signal": "interrupt"}, _NO_WAIT)
    assert time.monotonic() - begin < 0.5 and not caught.value.retryable
    assert workers[0].signals == []
    workers[0].shell("command_returned", result={"success": True, "code": 0})
    workers[0].shell("shell_prompt", cwd="/w")
    write(service, session_id, {"type": "line", "text": "ls"}, _NO_WAIT)
    assert workers[0].writes[-1] == {"type": "line", "text": "ls"}


def test_agent_may_type_into_its_own_running_command(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    write(service, session_id, {"type": "line", "text": "python3"}, _NO_WAIT)
    workers[0].shell("command_started", command="python3")
    assert "started by user" not in read(service, session_id).text
    write(service, session_id, {"type": "line", "text": "1+1"}, _NO_WAIT)
    assert workers[0].writes[-1] == {"type": "line", "text": "1+1"}


def test_resize_is_forwarded_and_geometry_follows_screen(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    service.require(session_id, 1).resize(40, 120)
    assert workers[0].resizes == [(40, 120)]
    workers[0].screen(["$ "], size=(40, 120))
    assert geometry(service, session_id) == (40, 120)


# ---------------------------------------------------------------- 交接（HITL）


@pytest.fixture
def decisions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, str, str]]:
    """记录运行时代为提交的交接决定，不写队列。"""
    from app.service import depends

    calls: list[tuple[int, str, str, str]] = []

    class _Decisions:
        def submit(self, task_id, tool_call_id, decision, *, feedback=None, data=None, source="user"):
            calls.append((task_id, tool_call_id, decision, source))

    monkeypatch.setattr(depends, "get_tool_decision_service", lambda: _Decisions())
    return calls


def _begin(service: Any, session_id: str, tool_call_id: str = "call-handoff", reason: str = "password"):
    return service.require(session_id, 1).begin_handoff(reason, tool_call_id)


def test_handoff_is_a_session_hint_ended_by_the_decision_replay(
    monkeypatch, tmp_path, decisions
) -> None:
    """交接只登记在会话上（不改变输入仲裁、不登记延迟结果）；决定重放时报告交接以来的输出并清除。"""
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].screen(["$ sudo make install", "Password:"], cursor=(1, 9))
    shown = _begin(service, session_id, reason="Enter sudo password")
    assert shown.input_type == "handoff" and shown.input_summary == "Enter sudo password"
    entry = session_entries(service, 1)[0]
    assert entry["handoff"] == {"reason": "Enter sudo password", "toolCallId": "call-handoff"}
    assert deferred_registry.pending(1) == []
    with pytest.raises(TerminalSessionStateError):
        _begin(service, session_id, tool_call_id="call-other")

    workers[0].screen(["$ sudo make install", "Password:", "installed"], cursor=(2, 9))
    returned = service.require(session_id, 1).end_handoff("call-handoff")
    assert "handed the terminal back" in returned.text and "installed" in returned.text
    assert session_entries(service, 1)[0]["handoff"] is None
    assert decisions == []


def test_user_typing_pause_does_not_end_handoff(monkeypatch, tmp_path, decisions):
    """输密码时会停顿；交接只认「交还」（HITL 决定），不能因用户停手而结束。"""
    monkeypatch.setattr(Constant.Terminal, "USER_TYPING_IDLE_SECONDS", 0.1)
    service, _ = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    _begin(service, session_id)
    service.require(session_id, 1).user_input(b"secr")
    time.sleep(0.2)
    service.require(session_id, 1).user_input(b"et")
    time.sleep(0.2)
    assert session_entries(service, 1)[0]["handoff"] is not None
    assert decisions == []


def test_session_end_settles_the_handoff_on_the_users_behalf(monkeypatch, tmp_path, decisions):
    """shell 意外退出时代为落定那次调用，Agent 不会一直挂着；重放时报告会话已结束。"""
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    _begin(service, session_id)
    workers[0].exit(1)
    wait_until(lambda: len(decisions) == 1)
    assert decisions == [(1, "call-handoff", "reject", "runtime")]
    returned = service.require(session_id, 1).end_handoff("call-handoff")
    assert "ended before the user handed it back" in returned.text


def test_backend_shutdown_keeps_the_pending_handoff_decision(monkeypatch, tmp_path, decisions):
    """后端关闭不代为落定：等待中的决定按 HITL 契约保留，重启后交还会报告会话已不在。"""
    service, _ = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    _begin(service, session_id)
    service.shutdown()
    time.sleep(0.2)
    assert decisions == []


def test_read_command_returned_on_idle_shell_returns_at_once(monkeypatch, tmp_path):
    service, _ = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    result = read(
        service, session_id, wait=TerminalWait(until="command_returned", timeout_ms=30_000)
    )
    assert result.wait_result == {"until": "command_returned", "end_reason": "idle"}
    assert deferred_registry.pending(1) == []


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


# ---------------------------------------------------------------- 全屏程序（备用屏幕）


def open_less(worker: FakeWorker, input: dict[str, Any]) -> None:
    """模拟 less：line 进入全屏程序，text ``q`` 退回主屏，keys 原地重绘。"""
    if input["type"] == "line":
        worker.screen(["1", "2", "3", ":"], cursor=(3, 1), alternate=True)
    elif input["type"] == "keys":
        worker.screen(["4", "5", "6", ":"], cursor=(3, 1), alternate=True)
    elif input["type"] == "text" and input["text"] == "q":
        worker.screen(["$ echo a", "a", "$ seq 9 | less", "$ "], cursor=(3, 2))


def test_full_screen_app_returns_screen_view_and_keeps_line_numbers(monkeypatch, tmp_path):
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].on_write = open_less
    workers[0].screen(["$ echo a", "a", "$ "], cursor=(2, 2))
    before = read(service, session_id, start_line=1, limit=3).text

    opened = write(service, session_id, {"type": "line", "text": "seq 9 | less"})
    assert opened.screen and opened.lines is None
    assert "full-screen app" in opened.text and "s1│1\ns2│2\ns3│3\ns4│:" in opened.text
    assert "cursor s4" in opened.text

    # 原地重绘：write 返回整屏，而不是「输入后新增行」。
    scrolled = write(service, session_id, {"type": "keys", "keys": ["down"]})
    assert "s1│4\ns2│5\ns3│6" in scrolled.text
    # 逻辑行号不被屏幕行占用：start_line 仍读到进入前的主屏。
    frozen = read(service, session_id, start_line=1, limit=3)
    assert not frozen.screen and frozen.text.splitlines()[-3:] == before.splitlines()[-3:]
    assert read(service, session_id).screen

    closed = write(service, session_id, {"type": "text", "text": "q"})
    assert workers[0].writes[-1] == {"type": "text", "text": "q"}
    assert not closed.screen and "4│$ " in closed.text and "s1│" not in closed.text


def test_match_in_full_screen_app_scans_the_whole_screen(monkeypatch, tmp_path):
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].screen(["top", "PID CMD", "1 init"], cursor=(0, 0), alternate=True)
    result = read(service, session_id, wait=TerminalWait(until="match", pattern="init"))
    assert result.wait_result == {
        "until": "match", "end_reason": "matched", "matched_screen_row": 3
    }
    assert "wait=match → matched at s3" in result.text


def test_match_after_input_ignores_the_screen_before_the_redraw(monkeypatch, tmp_path):
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].screen(["1", "2", "3", ":"], cursor=(3, 1), alternate=True)
    thread, box = run_in_thread(lambda: write(
        service, session_id, {"type": "keys", "keys": ["down"]},
        TerminalWait(until="match", pattern="^2$", timeout_ms=2_000),
    ))
    time.sleep(0.2)
    assert thread.is_alive(), "输入前就可见的 2 不应命中"
    workers[0].screen(["2", "3", "4", ":"], cursor=(3, 1), alternate=True)
    thread.join(2)
    assert box["result"].wait_result["matched_screen_row"] == 1
    assert "s1│2\ns2│3\ns3│4" in box["result"].text


def test_user_input_in_full_screen_app_is_flagged_without_line_marks(monkeypatch, tmp_path):
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path, created_by="user")
    workers[0].screen(["~", "~", ":"], cursor=(2, 1), alternate=True)
    service.require(session_id, 1).user_input(b"j")
    text = read(service, session_id).text
    assert "user typed in this app" in text and "user typed lines" not in text
    assert "user typed" not in read(service, session_id, tail=3).text


def test_text_input_rejects_control_characters_and_empty() -> None:
    from pydantic import ValidationError

    from app.tools.terminal_runtime.inputs import InputText

    assert InputText.model_validate({"type": "text", "text": ":wq"}).text == ":wq"
    for bad in ["", "q\r", "\x1b"]:
        with pytest.raises(ValidationError):
            InputText.model_validate({"type": "text", "text": bad})


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
    session_id = str(session_entries(service, 1)[0]["sessionId"])
    began = time.monotonic()
    [entry] = session_entries(service, 1)
    assert entry["status"] == "starting"
    with pytest.raises(TerminalSessionStateError):
        service.require(session_id, 1).user_input(b"x")
    service.require(session_id, 1).resize(30, 100)
    assert time.monotonic() - began < 1
    assert workers[0].writes == [] and workers[0].resizes == []
    slow_start.release.set()
    thread.join(2)
    assert box["result"] == session_id
    assert session_entries(service, 1)[0]["status"] == "running"
    # 启动期间记下的尺寸在握手完成后补发。
    assert workers[0].resizes == [(30, 100)]


def test_close_during_handshake_disposes_after_start_returns(
    monkeypatch, tmp_path, slow_start
) -> None:
    service, workers = make_service(monkeypatch, worker_type=slow_start)
    thread, box = run_in_thread(lambda: start(service, tmp_path, created_by="user"))
    assert slow_start.entered.wait(2)
    session_id = str(session_entries(service, 1)[0]["sessionId"])
    closer, closed = run_in_thread(lambda: service.close(session_id, task_id=1, by="agent"))
    time.sleep(0.2)
    # 终态已迁移，但 worker 要等握手结束才关闭，不在 start 半途被关。
    assert session_entries(service, 1)[0]["status"] == "closed"
    assert not workers[0].closed and closer.is_alive()
    slow_start.release.set()
    thread.join(2)
    closer.join(2)
    assert workers[0].closed
    assert closed["result"].status == "closed"
    assert box["result"] == session_id


def test_user_input_backpressure_leaves_typing_and_marks_unchanged(monkeypatch, tmp_path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)

    def full(worker: FakeWorker, input: dict[str, Any]) -> None:
        raise TerminalWorkerBackpressureError("terminal worker input queue is full")

    workers[0].on_write = full
    with pytest.raises(TerminalWorkerBackpressureError):
        service.require(session_id, 1).user_input(b"a")
    text = read(service, session_id).text
    assert "user is typing" not in text and "user typed" not in text
