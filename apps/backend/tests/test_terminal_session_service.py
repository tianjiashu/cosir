import base64
from pathlib import Path
from typing import ClassVar

import pytest

from app.config.constant import Constant
from app.service.terminal.errors import (
    TerminalSessionError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionResyncRequiredError,
    TerminalSessionStateError,
    TerminalWorkerBackpressureError,
    TerminalWorkerSignalFailedError,
    TerminalWorkerUnavailableError,
)
from app.service.terminal.process_terminal_worker import ProcessTerminalWorker
from app.service.terminal.shell_resolver import ShellResolver
from app.service.terminal.terminal_session_service import (
    TerminalSessionService,
)


class FakeWorker:
    pid = 4321
    geometry: ClassVar = {"rows": 24, "columns": 80}
    capabilities = frozenset(
        {
            "signal_interrupt",
            "signal_eof_canonical",
            "signal_suspend",
            "input_ctrl_c",
            "input_ctrl_d",
        }
    )

    def __init__(self) -> None:
        self._callback = None
        self.writes: list[bytes] = []
        self.signals: list[str] = []
        self.closed = False
        self.screen = {
            "revision": 0,
            **self.geometry,
            "lines": [],
            "cursor": {"row": 0, "column": 0, "visible": True},
            "alternate": False,
        }

    def start(self, spec, cwd, on_event) -> None:
        self._callback = on_event
        on_event({"type": "handshake"})
        on_event({"type": "shell", "event": {"kind": "unavailable", "reason": "test_worker"}})

    def write(self, input) -> None:
        data = encode_test_input(input)
        self.writes.append(data)
        self.emit_output(data)

    def signal(self, signal_name: str) -> None:
        self.signals.append(signal_name)

    def close(self) -> None:
        self.closed = True

    def emit_output(self, data: bytes) -> None:
        assert self._callback is not None
        self._callback(
            {
                "type": "output",
                "data_base64": base64.b64encode(data).decode("ascii"),
            }
        )

        # service 单测注入已渲染历史，不模拟 ANSI；真实渲染由 Rust 与 PTY 集成测试验证。
        self._callback(
            {
                "type": "screen",
                "screen": self.screen,
                "history": [data.decode("utf-8", errors="replace")],
            }
        )

    def emit_exit(self, exit_code: int = 0) -> None:
        assert self._callback is not None
        self._callback({"type": "exit", "exit_code": exit_code})

    def emit_error(self, code: str) -> None:
        assert self._callback is not None
        self._callback({"type": "error", "code": code})


class BackpressureWorker(FakeWorker):
    def write(self, input) -> None:
        raise TerminalWorkerBackpressureError("input queue is full")


class SignalFailureWorker(FakeWorker):
    def signal(self, signal_name: str) -> None:
        raise TerminalWorkerSignalFailedError("terminal worker failed to deliver signal")


def make_service(
        monkeypatch, *, worker_type=FakeWorker, status_observer=None, ring_buffer_bytes=None
):
    """仅在测试中替换具体构造边界，收集每个会话独立创建的 worker。

    ``status_observer`` 是构造期必需项，不传时用 no-op 观察者占位（只关心记录行为、不关心
    回写的用例）；需要断言回写的用例自行传收集器。缓冲上限的事实源是
    ``Constant.Terminal.MAX_RING_BUFFER_BYTES``，测试只在该边界上收紧它（把窗口压到几字节
    以观察截断），不引入构造参数。
    """
    workers = []

    def construct(instance_id):
        worker = worker_type()
        worker.instance_id = instance_id
        workers.append(worker)
        return worker

    monkeypatch.setattr(
        "app.service.terminal.terminal_session_service.ProcessTerminalWorker", construct
    )
    if ring_buffer_bytes is not None:
        monkeypatch.setattr(Constant.Terminal, "MAX_RING_BUFFER_BYTES", ring_buffer_bytes)
    service = TerminalSessionService(status_observer=status_observer or (lambda _change: None))
    return service, workers


def output_texts(result: dict) -> list[str]:
    """取本页聚合后的 UTF-8 文本，验证观察游标连续性。"""

    return [result["history"]] if result["history"] else []


def test_session_agent_operations_and_readers_are_cursor_based(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    snapshot = service.start(
        task_id=7,
        workspace_id=8,
        workspace_root=str(tmp_path),
        cwd=".",
        run_id=1,
    )
    session_id = str(snapshot["session_id"])
    attachment = service.subscribe(session_id, task_id=7, after_seq=None)
    attachment.subscription.activate()

    result = service.write(
        session_id,
        task_id=7,
        input={"type": "line", "text": "你好"},
        wait_ms=100,
        operation_id="test-146",
    )

    assert workers[0].writes == ["你好\r".encode()]
    assert output_texts(result) == ["你好\r"]
    preview = attachment.subscription.get(0.1)
    assert preview["seq"] == 1
    assert base64.b64decode(preview["data_base64"]) == "你好\r".encode()
    reread = service.read(session_id, task_id=7, run_id=1, wait_ms=0)
    assert output_texts(reread) == output_texts(result)
    assert reread["screen"] == result["screen"]


def test_input_backpressure_rejects_each_write(tmp_path: Path, monkeypatch) -> None:
    task_id = 987654
    service, workers = make_service(monkeypatch, worker_type=BackpressureWorker)
    first_snapshot = service.start(
        task_id=task_id,
        workspace_id=8,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    second_snapshot = service.start(
        task_id=task_id,
        workspace_id=8,
        workspace_root=str(tmp_path),
        run_id=1,
    )

    with pytest.raises(TerminalWorkerBackpressureError):
        service.write(
            str(first_snapshot["session_id"]),
            task_id=task_id,
            input={"type": "line", "text": "echo"},
            operation_id="test-239",
        )
    with pytest.raises(TerminalWorkerBackpressureError):
        service.write(
            str(first_snapshot["session_id"]),
            task_id=task_id,
            input={"type": "line", "text": "echo"},
            operation_id="test-241",
        )
    with pytest.raises(TerminalWorkerBackpressureError):
        service.write(
            str(second_snapshot["session_id"]),
            task_id=task_id,
            input={"type": "line", "text": "echo"},
            operation_id="test-243",
        )
    service.shutdown()


def test_backpressure_does_not_consume_the_write_operation_id(tmp_path: Path, monkeypatch) -> None:
    service, workers = make_service(monkeypatch)
    session = service.start(task_id=7, workspace_id=8, workspace_root=str(tmp_path), run_id=1)
    worker = workers[0]
    write = worker.write
    try:
        monkeypatch.setattr(worker, "write", BackpressureWorker().write)
        with pytest.raises(TerminalWorkerBackpressureError):
            service.write(
                str(session["session_id"]),
                task_id=7,
                input={"type": "line", "text": "echo"},
                operation_id="op",
            )
        monkeypatch.setattr(worker, "write", write)
        for _ in range(2):
            service.write(
                str(session["session_id"]),
                task_id=7,
                input={"type": "line", "text": "echo"},
                operation_id="op",
            )
        assert worker.writes == [b"echo\r"]
    finally:
        service.shutdown()


def test_write_operation_id_is_idempotent_and_payload_bound(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    snapshot = service.start(
        task_id=7,
        workspace_id=8,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])

    first = service.write(
        session_id,
        task_id=7,
        operation_id="op-1",
        input={"type": "line", "text": "echo"},
        wait_ms=100,
    )
    second = service.write(
        session_id,
        task_id=7,
        operation_id="op-1",
        input={"type": "line", "text": "echo"},
        wait_ms=100,
    )

    assert not first["input"]["duplicate"]
    assert second["input"]["duplicate"]
    assert workers[0].writes == [b"echo\r"]
    with pytest.raises(TerminalSessionError, match="operation_id"):
        service.write(
            session_id,
            task_id=7,
            operation_id="op-1",
            input={"type": "line", "text": "different"},
            wait_ms=0,
        )


def test_worker_construction_failure_does_not_leave_starting_metadata(tmp_path, monkeypatch):
    monkeypatch.delenv(Constant.Terminal.WORKER_ENV, raising=False)
    service = TerminalSessionService(status_observer=lambda _change: None)

    with pytest.raises(TerminalWorkerUnavailableError, match="not configured"):
        service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    assert service.list_sessions(task_id=1, workspace_id=1) == []


def test_process_worker_rejects_input_before_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 新签名下构造需 worker 可执行已配置（路径来自环境变量），仅占位即可：测试不真正启动 sidecar。
    monkeypatch.setenv("CODING_AGENT_TERMINAL_WORKER", "terminal-worker")
    worker = ProcessTerminalWorker("instance")

    with pytest.raises(TerminalWorkerUnavailableError):
        worker.write({"type": "line", "text": "input"})


def test_preview_disconnect_does_not_close_session(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])
    attachment = service.subscribe(session_id, task_id=1, after_seq=None)
    service.unsubscribe(session_id, attachment.subscription)

    assert not workers[0].closed
    assert service.snapshot(session_id, task_id=1)["status"] == "running"


def test_session_ownership_and_ring_buffer_gap_are_explicit(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch, ring_buffer_bytes=4)
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])
    workers[0].emit_output(b"aa")
    workers[0].emit_output(b"bb")
    workers[0].emit_output(b"cc")

    with pytest.raises(TerminalSessionOwnershipError):
        service.read(session_id, task_id=2, run_id=1, wait_ms=0)
    result = service.read(session_id, task_id=1, run_id=1, wait_ms=0)
    assert result["history_truncated"]
    with pytest.raises(TerminalSessionResyncRequiredError):
        service.subscribe(session_id, task_id=1, after_seq=0)


def test_worker_exit_is_terminal_and_shutdown_closes_workers(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])
    attachment = service.subscribe(session_id, task_id=1, after_seq=None)
    attachment.subscription.activate()
    workers[0].emit_exit(17)

    assert service.snapshot(session_id, task_id=1)["status"] == "exited"
    assert attachment.subscription.get(0.1)["type"] == "exit"
    service.shutdown()
    assert workers[0].closed


def test_session_status_observer_receives_async_exit_after_start(
        monkeypatch, tmp_path: Path
) -> None:
    changes = []
    service, workers = make_service(monkeypatch, status_observer=changes.append)

    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=10,
    )
    workers[0].emit_exit(7)

    assert [change.status for change in changes] == ["running", "exited"]
    assert changes[-1].session_id == snapshot["session_id"]
    assert changes[-1].task_id == 1
    assert "created_by_run_id" not in snapshot
    assert changes[-1].exit_code == 7


def test_session_status_observer_receives_failure_and_explicit_close(
        monkeypatch, tmp_path: Path
) -> None:
    failure_changes = []
    failure_service, failure_workers = make_service(
        monkeypatch, status_observer=failure_changes.append
    )
    failure_service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=11,
    )
    failure_workers[0].emit_error("WORKER_FAILED")

    assert [change.status for change in failure_changes] == ["running", "failed"]

    close_changes = []
    close_service, close_workers = make_service(monkeypatch, status_observer=close_changes.append)
    close_snapshot = close_service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=12,
    )
    close_service.close(str(close_snapshot["session_id"]), task_id=1)

    assert [change.status for change in close_changes] == ["running", "closed"]


def test_terminal_completion_is_retired_from_active_registry(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])

    workers[0].emit_exit(0)

    assert session_id not in service._registry
    assert session_id in service._terminal_history
    assert service.snapshot(session_id, task_id=1)["status"] == "exited"


def test_signal_delivery_failure_is_returned_without_false_success(
        monkeypatch, tmp_path: Path
) -> None:
    service, workers = make_service(monkeypatch, worker_type=SignalFailureWorker)
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )

    with pytest.raises(TerminalWorkerSignalFailedError):
        service.signal(
            str(snapshot["session_id"]),
            task_id=1,
            signal_name="interrupt",
        )

    assert service.snapshot(str(snapshot["session_id"]), task_id=1)["status"] == "running"


def test_signal_ack_can_follow_output_on_the_worker_reader(tmp_path: Path, monkeypatch) -> None:
    """Worker 同一 reader 先投递输出再确认信号，service 不得持锁阻断确认。"""
    import threading

    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    worker = workers[0]
    acknowledged = threading.Event()

    def read_output_then_ack():
        worker.emit_output(b"before signal acknowledgement")
        acknowledged.set()

    reader = threading.Thread(target=read_output_then_ack)

    def signal(_name):
        reader.start()
        if not acknowledged.wait(2):
            raise TerminalWorkerUnavailableError("signal acknowledgement blocked by output")

    monkeypatch.setattr(worker, "signal", signal)
    try:
        result = service.signal(session_id, task_id=1, signal_name="interrupt")
        assert result["status"] == "running"
        assert not worker.closed
        assert output_texts(service.read(session_id, task_id=1, run_id=1, wait_ms=0)) == [
            "before signal acknowledgement"
        ]
    finally:
        reader.join(timeout=3)
        service.shutdown()


def test_close_can_interrupt_an_inflight_signal(tmp_path: Path, monkeypatch) -> None:
    """关闭唤醒未确认信号后，迟到的信号失败不能覆盖已确定的关闭原因。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    worker = workers[0]
    entered = threading.Event()
    closed = threading.Event()

    def signal(_name):
        entered.set()
        assert closed.wait(2)
        raise TerminalWorkerUnavailableError("worker closed while awaiting signal")

    def close():
        worker.closed = True
        closed.set()

    monkeypatch.setattr(worker, "signal", signal)
    monkeypatch.setattr(worker, "close", close)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                service.signal, session_id, task_id=1, signal_name="interrupt"
            )
            assert entered.wait(2)
            result = service.close(session_id, task_id=1)
            with pytest.raises(TerminalWorkerUnavailableError):
                pending.result(timeout=3)
        assert result["status"] == "closed"
        assert service.snapshot(session_id, task_id=1)["end_reason"] == "agent_closed"
    finally:
        service.shutdown()


def test_terminal_history_and_write_idempotency_cache_are_bounded(
        monkeypatch, tmp_path: Path
) -> None:
    service, workers = make_service(monkeypatch)
    session_ids: list[str] = []
    for index in range(Constant.Terminal.MAX_TERMINAL_HISTORY_SESSIONS + 1):
        snapshot = service.start(
            task_id=1,
            workspace_id=1,
            workspace_root=str(tmp_path),
            run_id=index + 1,
        )
        session_ids.append(str(snapshot["session_id"]))
        workers[-1].emit_exit(0)

    assert len(service._terminal_history) == Constant.Terminal.MAX_TERMINAL_HISTORY_SESSIONS
    assert session_ids[0] not in service._terminal_history

    active = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=10_000,
    )
    for index in range(Constant.Terminal.MAX_WRITE_OPERATION_CACHE + 1):
        service.write(
            str(active["session_id"]),
            task_id=1,
            operation_id=f"operation-{index}",
            input={"type": "line", "text": "x"},
            wait_ms=0,
        )

    assert (
            len(service._registry[str(active["session_id"])]._accepted_writes)
            == Constant.Terminal.MAX_WRITE_OPERATION_CACHE
    )
    service.shutdown()


def test_fatal_worker_error_is_not_projected_as_shell_exit(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])
    workers[0].emit_error("PTY_WRITE_FAILED")

    current = service.snapshot(session_id, task_id=1)
    assert current["status"] == "failed"
    assert current["end_reason"] == "worker_pty_write_failed"
    assert workers[0].closed


def test_task_deletion_releases_worker_and_metadata_hook(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])

    assert service.delete_task_sessions([1]) == 1
    assert workers[0].closed
    with pytest.raises(TerminalSessionNotFoundError):
        service.snapshot(session_id, task_id=1)


def test_task_deletion_closes_all_runs_and_blocks_late_start(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    first = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=9)
    service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=10)
    other = service.start(task_id=2, workspace_id=1, workspace_root=str(tmp_path), run_id=11)

    assert service.delete_task_sessions([1]) == 2
    assert all(worker.closed for worker in workers[:2])
    assert not workers[2].closed
    with pytest.raises(TerminalSessionNotFoundError):
        service.snapshot(str(first["session_id"]), task_id=1)
    with pytest.raises(TerminalSessionStateError, match="closing"):
        service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=12)
    assert service.snapshot(str(other["session_id"]), task_id=2)["status"] == "running"
    service.shutdown()


def test_shell_resolver_has_platform_specific_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.service.terminal.shell_resolver.shutil.which", lambda value: value)

    windows = ShellResolver.resolve("auto", system="Windows")
    cmd = ShellResolver.resolve("cmd", system="Windows")
    pwsh = ShellResolver.resolve("pwsh", system="Windows")
    posix = ShellResolver.resolve("auto", system="Linux", environ={"SHELL": "/bin/bash"})

    assert windows.kind == "powershell"
    assert windows.argv[-1] == "-NoLogo"
    assert cmd.kind == "cmd"
    assert cmd.argv[-2:] == ("/Q", "/D")
    assert pwsh.kind == "pwsh"
    assert pwsh.argv[-1] == "-NoLogo"
    assert posix.kind == "bash"


@pytest.mark.parametrize(
    "requested, system",
    [
        ("auto", "Windows"),
        ("cmd", "Windows"),
        ("pwsh", "Windows"),
        ("auto", "Darwin"),
        ("zsh", "Darwin"),
        ("/custom/shell", "Linux"),
    ],
)
def test_shell_resolver_builds_disabled_integration_into_each_launch_spec(
        monkeypatch, requested, system
):
    monkeypatch.setattr("app.service.terminal.shell_resolver.shutil.which", lambda value: value)
    spec = ShellResolver.resolve(
        requested, system=system, environ={"SHELL": "/bin/zsh"}, integration="off"
    )
    assert spec.integration == "off"


@pytest.mark.asyncio
async def test_preview_websocket_rejects_input_after_attach(monkeypatch, tmp_path: Path) -> None:
    from app.api.terminal_api import terminal_preview_stream

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages = [
                {
                    "type": "websocket.receive",
                    "text": '{"type":"attach","after_seq":null}',
                },
                {
                    "type": "websocket.receive",
                    "text": '{"type":"write","data":"x"}',
                },
            ]
            self.sent: list[dict[str, object]] = []
            self.closed_with: tuple[int, str] | None = None

        async def accept(self) -> None:
            return None

        async def receive(self) -> dict[str, object]:
            return self.messages.pop(0)

        async def send_json(self, payload: dict[str, object]) -> None:
            self.sent.append(payload)

        async def close(self, code: int, reason: str) -> None:
            self.closed_with = (code, reason)

    service, workers = make_service(monkeypatch)
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    websocket = FakeWebSocket()

    await terminal_preview_stream(websocket, 1, str(snapshot["session_id"]), service)

    assert any(item["type"] == "protocol_error" for item in websocket.sent)
    assert websocket.closed_with is not None and websocket.closed_with[0] == 1008
    assert workers[0].writes == []


# 本文件全部用例都需要真实后端环境（隔离主库 + 进程级单例）。
pytestmark = pytest.mark.usefixtures("backend_env")


def test_write_retry_after_output_gap_does_not_repeat_input(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch, ring_buffer_bytes=4)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    worker = workers[0]
    worker.emit_output(b"aa")
    worker.emit_output(b"bb")
    first = service.write(
        session_id,
        task_id=1,
        operation_id="op",
        input={"type": "line", "text": "x"},
    )
    retry = service.write(
        session_id,
        task_id=1,
        operation_id="op",
        input={"type": "line", "text": "x"},
    )
    assert first["history_truncated"]
    assert retry["history"] == first["history"]
    assert worker.writes == [b"x\r"]
    assert retry["input"]["duplicate"]
    service.shutdown()


def test_write_retry_after_read_failure_keeps_acceptance(tmp_path: Path, monkeypatch) -> None:
    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    runtime = service._registry[session_id]
    original_read = runtime._observe

    def fail_read(*args, **kwargs):
        raise RuntimeError("read failed after acceptance")

    monkeypatch.setattr(runtime, "_observe", fail_read)
    with pytest.raises(RuntimeError, match="read failed"):
        service.write(
            session_id,
            task_id=1,
            operation_id="op",
            input={"type": "line", "text": "x"},
        )
    monkeypatch.setattr(runtime, "_observe", original_read)
    service.write(
        session_id,
        task_id=1,
        operation_id="op",
        input={"type": "line", "text": "x"},
        wait_ms=0,
    )
    assert workers[0].writes == [b"x\r"]
    service.shutdown()


def test_write_retry_after_shell_exit_keeps_acceptance(tmp_path: Path, monkeypatch) -> None:
    """接受输入后读取失败，期间 shell 已退出；重试仍返回原会话而不再次入队。"""
    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    worker = workers[0]
    runtime = service._registry[session_id]
    read = runtime._observe

    def exit_then_fail(*args, **kwargs):
        worker.emit_exit(7)
        raise RuntimeError("output read failed")

    try:
        monkeypatch.setattr(runtime, "_observe", exit_then_fail)
        with pytest.raises(RuntimeError, match="output read failed"):
            service.write(
                session_id,
                task_id=1,
                operation_id="op",
                input={"type": "line", "text": "exit 7"},
            )
        monkeypatch.setattr(runtime, "_observe", read)
        result = service.write(
            session_id,
            task_id=1,
            operation_id="op",
            input={"type": "line", "text": "exit 7"},
        )
        assert result["state"]["session_status"] == "exited"
        assert worker.writes == [b"exit 7\r"]
        with pytest.raises(TerminalSessionError, match="different input"):
            service.write(
                session_id,
                task_id=1,
                operation_id="op",
                input={"type": "line", "text": "exit 0"},
            )
        assert len(workers) == 1
    finally:
        service.shutdown()


def test_concurrent_write_retries_enqueue_once(monkeypatch, tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: service.write(
                    session_id,
                    task_id=1,
                    operation_id="op",
                    input={"type": "line", "text": "x"},
                ),
                range(16),
            )
        )
    assert sum(not result["input"]["duplicate"] for result in results) == 1
    assert workers[0].writes == [b"x\r"]
    service.shutdown()


def test_cancel_before_acceptance_leaves_session_usable(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    with pytest.raises(TerminalSessionStateError, match="not accepted"):
        service.write(
            session_id,
            task_id=1,
            operation_id="op",
            input={"type": "line", "text": "x"},
            is_cancelled=lambda: True,
        )
    assert workers[0].writes == []
    service.write(session_id, task_id=1, operation_id="op", input={"type": "line", "text": "x"})
    assert workers[0].writes == [b"x\r"]
    assert not workers[0].closed
    service.shutdown()


def test_cancel_after_acceptance_does_not_repeat_input(tmp_path: Path, monkeypatch) -> None:
    import threading

    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    cancelled = threading.Event()
    worker = workers[0]

    def accept_then_cancel(input):
        worker.writes.append(encode_test_input(input))
        cancelled.set()

    monkeypatch.setattr(worker, "write", accept_then_cancel)
    result = service.write(
        session_id,
        task_id=1,
        operation_id="op",
        input={"type": "line", "text": "x"},
        wait_ms=30000,
        is_cancelled=cancelled.is_set,
    )
    assert result["state"]["session_status"] == "running"
    cancelled.clear()
    service.write(
        session_id,
        task_id=1,
        operation_id="op",
        input={"type": "line", "text": "x"},
        wait_ms=0,
    )
    assert worker.writes == [b"x\r"]
    service.shutdown()


def test_list_sessions_is_task_scoped_and_keeps_recent_exits(monkeypatch, tmp_path: Path) -> None:
    service, workers = make_service(monkeypatch)
    first = service.start(
        task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1, label="dev server"
    )
    second = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=2)
    service.start(task_id=2, workspace_id=1, workspace_root=str(tmp_path), run_id=3)
    sessions = service.list_sessions(task_id=1, workspace_id=1)
    assert [item["session_id"] for item in sessions] == [first["session_id"], second["session_id"]]
    assert sessions[0]["label"] == "dev server"
    assert not {"worker_pid", "worker_instance_id", "shell_executable"} & sessions[0].keys()
    assert service.list_sessions(task_id=1, workspace_id=2) == []
    workers[0].emit_exit(0)
    sessions = service.list_sessions(task_id=1, workspace_id=1)
    assert (
            next(item for item in sessions if item["session_id"] == first["session_id"])["status"]
            == "exited"
    )
    service.delete_task_sessions([1])
    assert service.list_sessions(task_id=1, workspace_id=1) == []
    service.shutdown()


@pytest.mark.asyncio
async def test_task_sessions_survive_run_end(monkeypatch, tmp_path: Path) -> None:
    """Run 结束不关闭终端：同一 Task 的后续 Run 仍能发现并写入。"""
    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    discovered = service.list_sessions(task_id=1, workspace_id=1)
    assert discovered[0]["session_id"] == session["session_id"]
    service.write(
        str(discovered[0]["session_id"]),
        task_id=1,
        input={"type": "line", "text": "next run"},
        wait_ms=0,
        operation_id="test-892",
    )
    assert workers[0].writes == [b"next run\r"]
    service.shutdown()
    assert workers[0].closed
    with pytest.raises(TerminalSessionStateError, match="shutting down"):
        service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=2)


@pytest.mark.parametrize("cleanup", ["delete", "shutdown"])
def test_cleanup_during_start_cannot_leave_a_worker(tmp_path: Path, monkeypatch, cleanup) -> None:
    """Task 删除或退出与慢启动交错时，清理必须等到 worker 建立后完成。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered = threading.Event()
    release = threading.Event()
    cleaning = threading.Event()

    class SlowWorker(FakeWorker):
        def start(self, spec, cwd, on_event):
            entered.set()
            assert release.wait(2)
            super().start(spec, cwd, on_event)

    service, workers = make_service(monkeypatch, worker_type=SlowWorker)
    original = service._close_matching

    def mark_cleanup(*args, **kwargs):
        cleaning.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_close_matching", mark_cleanup)
    with ThreadPoolExecutor(max_workers=2) as executor:
        starting = executor.submit(
            service.start, task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1
        )
        assert entered.wait(2)
        closing = (
            executor.submit(service.delete_task_sessions, [1])
            if cleanup == "delete"
            else executor.submit(service.shutdown)
        )
        assert cleaning.wait(2)
        release.set()
        with pytest.raises(TerminalSessionStateError, match="closing"):
            starting.result(timeout=2)
        closing.result(timeout=2)
    assert workers[0].closed
    assert service.list_sessions(task_id=1, workspace_id=1) == []
    service.shutdown()


def test_terminal_lifecycle_writes_structured_file_log(monkeypatch, tmp_path: Path) -> None:
    import json

    from app.config.logging.configuration import configure_logging, shutdown_logging

    log_dir = tmp_path / "logs"
    configure_logging(log_dir)
    service, _ = make_service(monkeypatch)
    try:
        session = service.start(task_id=7, workspace_id=8, workspace_root=str(tmp_path), run_id=9)
        service.delete_task_sessions([7])
    finally:
        service.shutdown()
        shutdown_logging()
    events = [
        json.loads(line)
        for path in log_dir.glob("backend-*.log")
        for line in path.read_text().splitlines()
    ]
    changes = [item for item in events if item.get("event") == "terminal_session_status_changed"]
    assert [item["data"]["status"] for item in changes] == ["running", "closed"]
    assert changes[-1]["data"]["session_id"] == session["session_id"]
    assert changes[-1]["data"]["task_id"] == 7
    assert changes[-1]["data"]["created_by_run_id"] == 9
    assert changes[-1]["data"]["reason"] == "task_deleted"
    started = next(item for item in events if item.get("event") == "terminal_worker_started")
    assert started["data"]["session_id"] == session["session_id"]
    assert started["data"]["worker_pid"] == 4321
    assert started["data"]["worker_instance_id"]
    assert started["data"]["shell_executable"]


def encode_test_input(input):
    if input["type"] == "line":
        return input["text"].encode() + b"\r"
    return {"ctrl_c": b"\x03", "ctrl_d": b"\x04", "ctrl_z": b"\x1a"}[input["key"]]


def test_shell_exit_ends_tracking_without_inventing_command_result(monkeypatch, tmp_path: Path):
    service, workers = make_service(monkeypatch)
    snapshot = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session = snapshot["session_id"]
    worker = workers[0]
    worker._callback({"type": "shell", "event": {"kind": "command_started", "command": "python"}})
    command = service.snapshot(session, task_id=1)["state"]["active_command_id"]
    worker.emit_exit(7)
    page = service.read(session, task_id=1, run_id=1, wait_ms=0)
    assert page["return_reason"] == "session_ended"
    assert page["latest_command"]["status"] == "tracking_ended"
    assert page["latest_command"]["command_id"] == command
    assert page["state"]["session_exit_code"] == 7
    assert "result" not in page["latest_command"]
    service.shutdown()


@pytest.mark.parametrize(
    "keys, supported, rejected",
    [
        ({"input_ctrl_c", "input_ctrl_d"}, "ctrl_d", "ctrl_z"),
        ({"input_ctrl_c", "input_ctrl_z"}, "ctrl_z", "ctrl_d"),
    ],
)
def test_control_keys_use_worker_capabilities_before_acceptance(
        monkeypatch, tmp_path, keys, supported, rejected
):
    class CapabilityWorker(FakeWorker):
        capabilities = frozenset(keys)

    service, workers = make_service(monkeypatch, worker_type=CapabilityWorker)
    snapshot = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session = snapshot["session_id"]
    try:
        assert set(snapshot["capabilities"]) == keys
        with pytest.raises(TerminalSessionError, match="not supported"):
            service.write(
                session, task_id=1, operation_id="rejected", input={"type": "key", "key": rejected}
            )
        assert not workers[0].writes
        result = service.write(
            session,
            task_id=1,
            operation_id="accepted",
            input={"type": "key", "key": supported},
            wait_ms=0,
        )
        assert result["input"]["accepted"]
        assert set(result["capabilities"]) == keys
        assert workers[0].writes == [encode_test_input({"type": "key", "key": supported})]
    finally:
        service.shutdown()


def test_read_waits_three_seconds_despite_output_and_returns_latest_screen(monkeypatch, tmp_path):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from app.service.terminal.errors import TerminalReadInProgressError

    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)[
        "session_id"
    ]
    entered = threading.Event()

    def cancellation_check():
        entered.set()
        return False

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            before = time.monotonic()
            future = pool.submit(
                service.read, session, task_id=1, run_id=1, is_cancelled=cancellation_check
            )
            assert entered.wait(1)
            with pytest.raises(TerminalReadInProgressError):
                service.read(session, task_id=1, run_id=1)
            # 输入不被 read 锁阻塞；真实更新也不能使 read 提前返回。
            service.write(
                session,
                task_id=1,
                operation_id="during-read",
                input={"type": "line", "text": "ok"},
                wait_ms=0,
            )
            workers[0]._callback(
                {
                    "type": "screen",
                    "screen": {
                        **workers[0].screen,
                        "revision": 1,
                        "lines": [{"text": "LATEST?", "wrapped": False}],
                    },
                    "history": [],
                }
            )
            assert not future.done()
            result = future.result(timeout=5)
            assert time.monotonic() - before >= 3.0
            assert result["screen"]["text"] == "LATEST?"
            assert result["return_reason"] == "wait_elapsed"
    finally:
        service.shutdown()


def test_repeat_error_keeps_command_alive_and_changed_output_or_run_recovers(monkeypatch, tmp_path):
    from app.service.terminal.errors import TerminalRepeatedReadError

    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)[
        "session_id"
    ]
    worker = workers[0]
    worker._callback(
        {"type": "shell", "event": {"kind": "command_started", "command": "sleep 120"}}
    )
    try:
        # 服务单测用零等待隔离计数逻辑，公开工具下限另由 schema 和真实计时测试验证。
        first = service.read(session, task_id=1, run_id=1, wait_ms=0)
        assert "warning" not in first
        for count in range(1, 5):
            result = service.read(session, task_id=1, run_id=1, wait_ms=0)
            assert result["warning"]["unchanged_reads"] == count
            assert result["screen"] == first["screen"]
        with pytest.raises(TerminalRepeatedReadError):
            service.read(session, task_id=1, run_id=1, wait_ms=0)
        assert service.snapshot(session, task_id=1)["status"] == "running"
        assert not worker.closed and not worker.signals
        worker.emit_output(b"new heartbeat\n")
        recovered = service.read(session, task_id=1, run_id=1, wait_ms=0)
        assert recovered["latest_command"]["status"] == "executing"
        assert "warning" not in recovered
        fresh = service.read(session, task_id=1, run_id=2, wait_ms=0)
        assert "warning" not in fresh
        assert fresh["screen"] == recovered["screen"]
    finally:
        service.shutdown()


def test_cancelled_read_does_not_count_or_leave_read_locked(monkeypatch, tmp_path):
    service, _ = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)[
        "session_id"
    ]
    try:
        for _ in range(8):
            result = service.read(session, task_id=1, run_id=1, is_cancelled=lambda: True)
            assert result["return_reason"] == "cancelled"
            assert "warning" not in result
        result = service.read(session, task_id=1, run_id=1, wait_ms=0)
        assert "warning" not in result
    finally:
        service.shutdown()


def test_write_and_read_keep_bounded_latest_history_without_consumption(monkeypatch, tmp_path):
    from app.service.terminal.terminal_observation import json_size

    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)[
        "session_id"
    ]
    try:
        for _ in range(10):
            workers[0].emit_output(b"old " * 4096)
        workers[0].emit_output(b"LATEST\n")
        first = service.write(
            session,
            task_id=1,
            operation_id="answer",
            input={"type": "line", "text": "yes"},
            wait_ms=0,
        )
        assert first["history"].endswith("LATEST\nyes\r")
        assert first["history_truncated"]
        result = service.read(session, task_id=1, run_id=1, wait_ms=0)
        assert result["history"].endswith("LATEST\nyes\r")
        assert json_size(result) <= Constant.Tools.MAX_OUTPUT_CHARS
        assert not {"next_cursor", "has_more", "gap", "events"} & result.keys()
    finally:
        service.shutdown()
