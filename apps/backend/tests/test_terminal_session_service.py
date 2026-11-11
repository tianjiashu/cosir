"""终端服务契约：按行读取、条件等待（事件驱动）、Agent 填入准入、后台等待与 snapshot 投影。

worker 用假替身按协议喂事件；真实 PTY 行为见 ``test_terminal_shell_integration.py``。Agent 对交互
终端只能「填入一行、不执行」：原有用 ``write`` 整行/按键驱动 shell 的用例改成用户输入
（``runtime.user_input``，与终端 WS 的 raw 帧同一路径）。
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
    active_session_ids,
    geometry,
    make_service,
    never,
    run_in_thread,
    session_entries,
    session_view,
    start,
    user_enter,
    wait_until,
)

from app.config.constant import Constant
from app.core.tool.background.background_registry import background_registry
from app.tools.terminal_runtime.errors import (
    TerminalIntegrationRequiredError,
    TerminalPrefillRejectedError,
    TerminalSessionCapacityError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionStateError,
    TerminalWorkerBackpressureError,
    TerminalWorkerUnavailableError,
)
from app.tools.terminal_runtime.observation.wait import TerminalWait


@pytest.fixture(autouse=True)
def _drop_entries() -> Any:
    yield
    for task_id in range(1, 10):
        background_registry.drop_task(task_id)


def prefill(service: Any, session_id: str, command: str, *, task_id: int = 1) -> Any:
    return service.require(session_id, task_id).prefill(
        command, run_id=1, tool_call_id=_call_id()
    )


def read(service: Any, session_id: str, **kwargs: Any) -> Any:
    return service.require(session_id, 1).read(is_cancelled=never, **kwargs)


def _call_id() -> str:
    return f"call-{time.monotonic_ns()}"


def prompt(worker: FakeWorker, cwd: str = "/w") -> None:
    """shell 集成报告「已回到提示符」：这是 Agent 填入的前提。"""
    worker.shell("shell_prompt", cwd=cwd)


# ---------------------------------------------------------------- 生命周期与归属


def test_start_list_close_and_ownership(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    meta = session_view(service, session_id)
    assert meta.summary.startswith(f"session {session_id} · zsh · running")
    with pytest.raises(TerminalSessionOwnershipError):
        service.require(session_id, 2)
    with pytest.raises(TerminalSessionNotFoundError):
        service.close("missing", task_id=1)
    closed = service.close(session_id, task_id=1)
    assert closed.status == "closed"
    assert workers[0].closed
    assert active_session_ids(service) == []
    assert session_entries(service, 1)[0]["status"] == "closed"
    assert service.active_session_count() == 0


def test_active_session_capacity_is_global(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Constant.Terminal, "MAX_ACTIVE_SESSIONS", 2)
    service, _ = make_service(monkeypatch)
    start(service, tmp_path)
    start(service, tmp_path, task_id=2)
    with pytest.raises(TerminalSessionCapacityError):
        start(service, tmp_path, task_id=3)
    assert service.active_session_count() == 2


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


def test_worker_disconnect_fails_session_but_not_after_exit(monkeypatch, tmp_path) -> None:
    """读线程补发的 ``disconnected`` 把活会话收敛为 failed；shell 已退出后到达的断开不改写终态。"""
    service, workers = make_service(monkeypatch)
    start(service, tmp_path)
    workers[0].emit({"type": "disconnected"})
    wait_until(lambda: workers[0].closed)
    [entry] = session_entries(service, 1)
    assert (entry["status"], entry["endReason"]) == ("failed", "worker_disconnected")

    exited = start(service, tmp_path)
    workers[1].exit(0)
    workers[1].emit({"type": "disconnected"})
    wait_until(lambda: workers[1].closed)
    entry = next(e for e in session_entries(service, 1) if e["sessionId"] == exited)
    assert (entry["status"], entry["exitCode"]) == ("exited", 0)


def test_worker_unavailable_on_fill_does_not_end_session(monkeypatch, tmp_path) -> None:
    """worker 不可用只作为这次调用的错误抛出，不结束会话；按键按会话已结束回报（不无声丢键）。"""
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    prompt(workers[0])

    def unavailable(input: dict[str, Any]) -> None:
        raise TerminalWorkerUnavailableError("terminal worker is not ready")

    workers[0].write = unavailable  # type: ignore[method-assign]
    with pytest.raises(TerminalWorkerUnavailableError):
        prefill(service, session_id, "ls")
    assert service.require(session_id, 1).user_input(b"a") == "session_ended"
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
    ended = [start(service, tmp_path) for _ in range(12)]
    for session_id in ended:
        service.close(session_id, task_id=1)
    live = start(service, tmp_path)
    entries = session_entries(service, 1)
    assert entries[0]["sessionId"] == live
    assert len(entries) == 11
    assert [e["sessionId"] for e in entries[1:]] == ended[2:]
    assert set(entries[0]) == {
        "sessionId", "shellKind", "status", "endReason", "exitCode",
        "integration", "commandRunning",
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

    from app.tools.tool_models.terminal_tools_args import TerminalReadArgs

    with pytest.raises(ValidationError, match="tail"):
        TerminalReadArgs.model_validate({"terminal_id": "s", "pattern": "x", "tail": 5})
    with pytest.raises(ValidationError, match="invalid pattern"):
        TerminalReadArgs.model_validate({"terminal_id": "s", "pattern": "("})


def test_user_typed_lines_are_reported_once(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].screen(["a", "b", "$ "], cursor=(2, 2))
    assert service.require(session_id, 1).user_input(b"l") == "accepted"
    assert workers[0].writes[-1] == {"type": "raw", "data_base64": "bA=="}
    assert "user typed lines 3-3" in read(service, session_id).text
    assert "user typed" not in read(service, session_id, tail=3).text


# ---------------------------------------------------------------- 条件等待


def test_command_returned_without_integration_is_rejected(monkeypatch, tmp_path: Path) -> None:
    service, _ = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    with pytest.raises(TerminalIntegrationRequiredError):
        read(service, session_id, wait=TerminalWait(until="command_returned"))


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
    service, _ = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    began = time.monotonic()
    quiet = read(
        service, session_id, wait=TerminalWait(until="output_quiet", quiet_ms=200, timeout_ms=3000)
    )
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


# ---------------------------------------------------------------- Agent 填入


def test_prefill_fills_the_prompt_without_running_it(monkeypatch, tmp_path: Path) -> None:
    """填入只把一行写进空提示符：worker 收到 prefill 帧，立即返回尾部，不等输出。"""
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    workers[0].screen(["$ "], cursor=(0, 2))
    prompt(workers[0])
    marks: list[dict[str, object]] = []
    runtime = service.require(session_id, 1)
    monkeypatch.setattr(runtime._stream, "publish", lambda name, **fields: marks.append(
        {"type": name, **fields}
    ))

    result = prefill(service, session_id, "echo hi")

    assert workers[0].writes == [{"type": "prefill", "text": "echo hi"}]
    assert result.prefilled == "echo hi"
    assert "input prefill `echo hi`" in result.text
    assert "runs only if the user presses Enter" in result.text
    assert {"type": "input_mark", "by": "agent"} in marks
    assert runtime._observation.prompt_has_input is True


def test_second_prefill_on_the_same_prompt_is_rejected(monkeypatch, tmp_path: Path) -> None:
    """同一提示符至多填一次：这替代了旧的 operation_id 幂等缓存。"""
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    prompt(workers[0])
    prefill(service, session_id, "echo first")
    with pytest.raises(TerminalPrefillRejectedError, match="already has input"):
        prefill(service, session_id, "echo second")
    assert len(workers[0].writes) == 1


def test_user_typing_on_the_prompt_blocks_prefill_until_the_next_prompt(
    monkeypatch, tmp_path: Path
) -> None:
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    prompt(workers[0])
    service.require(session_id, 1).user_input(b"ec")
    with pytest.raises(TerminalPrefillRejectedError, match="already has input"):
        prefill(service, session_id, "echo hi")
    prompt(workers[0])
    prefill(service, session_id, "echo hi")
    assert workers[0].writes[-1] == {"type": "prefill", "text": "echo hi"}


def test_prefill_rejected_while_a_command_runs_or_without_integration(
    monkeypatch, tmp_path: Path
) -> None:
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    prompt(workers[0])
    workers[0].shell("command_started", command="sleep 1")
    with pytest.raises(TerminalPrefillRejectedError, match="command is running"):
        prefill(service, session_id, "ls")

    off_service, _ = make_service(monkeypatch)
    off_session = start(off_service, tmp_path, task_id=2)
    with pytest.raises(TerminalPrefillRejectedError, match="no shell integration"):
        prefill(off_service, off_session, "ls", task_id=2)


def test_prefill_on_an_ended_session_reports_the_session_state(monkeypatch, tmp_path: Path) -> None:
    """会话结束后填入按会话状态拒绝，不误报成「有命令在运行」，也不写 worker。"""
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    prompt(workers[0])
    workers[0].exit(0)
    with pytest.raises(TerminalSessionStateError, match="terminal session is exited"):
        prefill(service, session_id, "ls")
    assert workers[0].writes == []


def test_command_started_by_the_user_after_a_prefill(monkeypatch, tmp_path: Path) -> None:
    """Agent 只填行；回车由用户按，命令的发起方因此记为用户。"""
    service, workers = make_service(monkeypatch, worker_type=ReadyWorker)
    session_id = start(service, tmp_path)
    prompt(workers[0])
    prefill(service, session_id, "echo PREFILLED")
    user_enter(service.require(session_id, 1))
    # 真实 worker 的 command_started 在用户按键之后才到达；假替身按同一顺序显式发送。
    workers[0].shell("command_started", command="echo PREFILLED")
    assert "started by user" in read(service, session_id).text


def test_write_arguments_reject_line_breaks_and_control_characters() -> None:
    from pydantic import ValidationError

    from app.tools.tool_models.terminal_tools_args import TerminalWriteArgs

    assert TerminalWriteArgs(terminal_id="s", command="echo hi").command == "echo hi"
    for bad in ["a\nb", "a\rb", "a\x1bb", "a\x03b"]:
        with pytest.raises(ValidationError):
            TerminalWriteArgs(terminal_id="s", command=bad)
    with pytest.raises(ValidationError):
        TerminalWriteArgs.model_validate({"terminal_id": "s", "command": "x", "input": {}})


def test_resize_is_forwarded_and_geometry_follows_screen(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    service.require(session_id, 1).resize(40, 120)
    assert workers[0].resizes == [(40, 120)]
    workers[0].screen(["$ "], size=(40, 120))
    assert geometry(service, session_id) == (40, 120)


# ---------------------------------------------------------------- 全屏程序（备用屏幕）


def test_full_screen_app_reads_screen_view_and_keeps_line_numbers(monkeypatch, tmp_path):
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].screen(["$ echo a", "a", "$ "], cursor=(2, 2))
    before = read(service, session_id, start_line=1, limit=3).text

    # 用户在终端里打开了全屏程序：屏幕切到备用屏幕，主屏逻辑行冻结。
    workers[0].screen(["1", "2", "3", ":"], cursor=(3, 1), alternate=True)
    opened = read(service, session_id)
    assert opened.screen and opened.lines is None
    assert "full-screen app" in opened.text and "s1│1\ns2│2\ns3│3\ns4│:" in opened.text
    assert "cursor s4" in opened.text

    # 逻辑行号不被屏幕行占用：start_line 仍读到进入前的主屏。
    frozen = read(service, session_id, start_line=1, limit=3)
    assert not frozen.screen and frozen.text.splitlines()[-3:] == before.splitlines()[-3:]

    # 用户退出全屏程序后回到主屏视图。
    workers[0].screen(["$ echo a", "a", "$ "], cursor=(2, 2))
    back = read(service, session_id)
    assert not back.screen and "s1│" not in back.text


def test_match_in_full_screen_app_scans_the_whole_screen(monkeypatch, tmp_path):
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].screen(["top", "PID CMD", "1 init"], cursor=(0, 0), alternate=True)
    result = read(service, session_id, wait=TerminalWait(until="match", pattern="init"))
    assert result.wait_result == {
        "until": "match", "end_reason": "matched", "matched_screen_row": 3
    }
    assert "wait=match → matched at s3" in result.text


def test_user_input_in_full_screen_app_is_flagged_without_line_marks(monkeypatch, tmp_path):
    service, workers = make_service(monkeypatch)
    session_id = start(service, tmp_path)
    workers[0].screen(["~", "~", ":"], cursor=(2, 1), alternate=True)
    service.require(session_id, 1).user_input(b"j")
    text = read(service, session_id).text
    assert "user typed in this app" in text and "user typed lines" not in text
    assert "user typed" not in read(service, session_id, tail=3).text


# ---------------------------------------------------------------- 并发与握手


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
    thread, box = run_in_thread(lambda: start(service, tmp_path))
    assert slow_start.entered.wait(2)
    session_id = str(session_entries(service, 1)[0]["sessionId"])
    began = time.monotonic()
    [entry] = session_entries(service, 1)
    assert entry["status"] == "starting"
    # 启动期间的按键与尺寸照常交给 worker 的有序队列（握手后才发出，见 worker 客户端用例）。
    assert service.require(session_id, 1).user_input(b"x") == "accepted"
    service.require(session_id, 1).resize(30, 100)
    assert time.monotonic() - began < 1
    assert workers[0].resizes == [(30, 100)]
    assert [w["type"] for w in workers[0].writes] == ["raw"]
    slow_start.release.set()
    thread.join(2)
    assert box["result"] == session_id
    assert session_entries(service, 1)[0]["status"] == "running"


def test_close_during_handshake_disposes_after_start_returns(
    monkeypatch, tmp_path, slow_start
) -> None:
    service, workers = make_service(monkeypatch, worker_type=slow_start)
    thread, box = run_in_thread(lambda: start(service, tmp_path))
    assert slow_start.entered.wait(2)
    session_id = str(session_entries(service, 1)[0]["sessionId"])
    closer, closed = run_in_thread(lambda: service.close(session_id, task_id=1))
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
