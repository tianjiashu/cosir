import base64
from pathlib import Path
from queue import SimpleQueue
from typing import cast

import pytest
from langchain_core.messages import AIMessage

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
from app.service.terminal.shell_resolver import ShellResolver
from app.service.terminal.terminal_session_service import (
    TerminalReadResult,
    TerminalSessionService,
)
from app.service.terminal.worker import ProcessTerminalWorker
from app.task_runtime.task_runtime_space import TaskRuntimeSpace
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class FakeWorker:
    pid = 4321
    capabilities = frozenset({"signal_interrupt", "signal_eof_canonical", "signal_suspend"})

    def __init__(self) -> None:
        self._callback = None
        self.writes: list[bytes] = []
        self.signals: list[str] = []
        self.closed = False

    def start(self, spec, cwd, on_event) -> None:
        self._callback = on_event
        on_event({"type": "handshake"})

    def write(self, data: bytes) -> None:
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

    def emit_exit(self, exit_code: int = 0) -> None:
        assert self._callback is not None
        self._callback({"type": "exit", "exit_code": exit_code})

    def emit_error(self, code: str) -> None:
        assert self._callback is not None
        self._callback({"type": "error", "code": code})


class FakeWorkerFactory:
    def __init__(self) -> None:
        self.workers: list[FakeWorker] = []

    def create(self, instance_id: str) -> FakeWorker:
        worker = FakeWorker()
        self.workers.append(worker)
        return worker


class BackpressureWorker(FakeWorker):
    def write(self, data: bytes) -> None:
        raise TerminalWorkerBackpressureError("input queue is full")


class BackpressureWorkerFactory:
    def __init__(self) -> None:
        self.workers: list[BackpressureWorker] = []

    def create(self, instance_id: str) -> BackpressureWorker:
        worker = BackpressureWorker()
        self.workers.append(worker)
        return worker


class SignalFailureWorker(FakeWorker):
    def signal(self, signal_name: str) -> None:
        raise TerminalWorkerSignalFailedError("terminal worker failed to deliver signal")


class SignalFailureWorkerFactory:
    def __init__(self) -> None:
        self.workers: list[SignalFailureWorker] = []

    def create(self, instance_id: str) -> SignalFailureWorker:
        worker = SignalFailureWorker()
        self.workers.append(worker)
        return worker


class FailingWorkerFactory:
    def create(self, instance_id: str):
        raise RuntimeError("worker unavailable")


def make_service(*, ring_buffer_bytes: int = 1024 * 1024):
    factory = FakeWorkerFactory()
    service = TerminalSessionService(
        worker_factory=factory,
        ring_buffer_bytes=ring_buffer_bytes,
    )
    return service, factory


def output_texts(result: TerminalReadResult) -> list[str]:
    """取模型可见的 UTF-8 文本帧，用于断言游标语义。"""

    frames = cast("list[dict[str, object]]", result.to_dict()["output"])
    return [str(frame["data"]) for frame in frames]


def test_session_agent_operations_and_readers_are_cursor_based(tmp_path: Path) -> None:
    service, factory = make_service()
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
        data="你好\n".encode(),
        after_seq=0,
        wait_ms=100,
    )

    assert factory.workers[0].writes == ["你好\n".encode()]
    assert result.output[0]["seq"] == 1
    assert "next_seq" not in result.to_dict()
    assert attachment.subscription.get(0.1) == {
        key: value for key, value in result.output[0].items() if key != "agent_data"
    }
    assert service.read(session_id, task_id=7, after_seq=0, wait_ms=0).output == result.output


def test_cursor_continues_from_the_last_returned_frame(tmp_path: Path) -> None:
    """续读使用最后一帧的 seq，工具结果无需暴露内部输出水位。"""

    service, factory = make_service()
    snapshot = service.start(
        task_id=7,
        workspace_id=8,
        workspace_root=str(tmp_path),
        cwd=".",
        run_id=1,
    )
    session_id = str(snapshot["session_id"])
    worker = factory.workers[0]

    # 一行输出在 ConPTY 上常被切成「内容帧 + 换行帧」两帧。
    worker.emit_output(b"line-1")
    worker.emit_output(b"\r\n")

    first = service.read(session_id, task_id=7, after_seq=None, wait_ms=0)
    assert output_texts(first) == ["line-1", "\r\n"]
    first_cursor = first.output[-1]["seq"]

    worker.emit_output(b"line-2")
    worker.emit_output(b"\r\n")

    applied = service.read(session_id, task_id=7, after_seq=first_cursor, wait_ms=0)
    assert output_texts(applied) == ["line-2", "\r\n"]
    applied_cursor = applied.output[-1]["seq"]

    worker.emit_output(b"line-3")
    worker.emit_output(b"\r\n")

    continued = service.read(session_id, task_id=7, after_seq=applied_cursor, wait_ms=0)
    assert output_texts(continued) == ["line-3", "\r\n"]


def test_agent_read_does_not_apply_a_second_output_byte_budget(tmp_path: Path) -> None:
    service, factory = make_service()
    snapshot = service.start(
        task_id=7,
        workspace_id=8,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])

    for _ in range(5):
        factory.workers[0].emit_output(b"x" * (16 * 1024))

    result = service.read(session_id, task_id=7, after_seq=0, wait_ms=0)

    assert len(result.output) == 5
    assert "truncated" not in result.to_dict()


def test_input_backpressure_does_not_notify_the_creator_run(tmp_path: Path, monkeypatch) -> None:
    task_id = 987654
    task_space = _queue_space(task_id)
    monkeypatch.setattr(task_runtime_spaces, "get_or_create", lambda _task_id: task_space)
    task_space.take_deferred_system_messages()
    factory = BackpressureWorkerFactory()
    service = TerminalSessionService(worker_factory=factory)
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
        service.write(str(first_snapshot["session_id"]), task_id=task_id, data=b"echo\r")
    with pytest.raises(TerminalWorkerBackpressureError):
        service.write(str(first_snapshot["session_id"]), task_id=task_id, data=b"echo\r")
    with pytest.raises(TerminalWorkerBackpressureError):
        service.write(str(second_snapshot["session_id"]), task_id=task_id, data=b"echo\r")
    first_messages = task_space.take_deferred_system_messages(run_id=1)

    assert first_messages == []
    assert task_space.take_deferred_system_messages() == []
    service.shutdown()


def test_backpressure_does_not_consume_the_write_operation_id(tmp_path: Path, monkeypatch) -> None:
    service, factory = make_service()
    session = service.start(task_id=7, workspace_id=8, workspace_root=str(tmp_path), run_id=1)
    worker = factory.workers[0]
    write = worker.write
    try:
        monkeypatch.setattr(worker, "write", BackpressureWorker().write)
        with pytest.raises(TerminalWorkerBackpressureError):
            service.write(str(session["session_id"]), task_id=7, data=b"echo\r", operation_id="op")
        monkeypatch.setattr(worker, "write", write)
        for _ in range(2):
            service.write(str(session["session_id"]), task_id=7, data=b"echo\r", operation_id="op")
        assert worker.writes == [b"echo\r"]
    finally:
        service.shutdown()


def test_deferred_system_messages_drop_stale_run_scoped_messages() -> None:
    task_space = _queue_space(987655)
    task_space.take_deferred_system_messages()
    task_space.defer_system_message(AIMessage(content="old", additional_kwargs={"run_id": 1}))
    task_space.defer_system_message(AIMessage(content="current", additional_kwargs={"run_id": 2}))

    messages = task_space.take_deferred_system_messages(run_id=2)

    assert [message.content for message in messages] == ["system_hint:current"]
    assert task_space.take_deferred_system_messages() == []


def test_take_only_discards_stale_run_scoped_messages_keeps_task_and_other_run() -> None:
    """取队列只丢弃「非当前 run」的 run 绑定消息，task 级与其它 run 的消息按规则留存。

    队列里同时存在三种消息：绑定旧 run_id=2 的（edit/重跑后属于过期反馈，必须丢弃）、
    无 run_id 的 task 级修复提示（任何 run 都可消费，必须保留）、绑定 run_id=3 的
    （尚未被消费，必须保留）。以 run_id=3 取队列时只应拿到后两者，且旧 run_id=2 的消息
    不得再被后续任何一次取队列看到。
    """

    task_space = _queue_space(987656)
    task_space.take_deferred_system_messages()
    task_space.defer_system_message(
        AIMessage(content="previous run feedback", additional_kwargs={"run_id": 2})
    )
    task_space.defer_system_message(AIMessage(content="task scoped"))
    task_space.defer_system_message(AIMessage(content="other run", additional_kwargs={"run_id": 3}))

    remaining = task_space.take_deferred_system_messages(run_id=3)

    assert [message.content for message in remaining] == [
        "system_hint:task scoped",
        "system_hint:other run",
    ]
    # 旧 run_id=2 的消息已在上面那次取队列时被丢弃，不会被任何后续 run 再消费。
    assert task_space.take_deferred_system_messages(run_id=2) == []


def test_write_operation_id_is_idempotent_and_payload_bound(tmp_path: Path) -> None:
    service, factory = make_service()
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
        data=b"echo\n",
        after_seq=0,
        wait_ms=100,
    )
    second = service.write(
        session_id,
        task_id=7,
        operation_id="op-1",
        data=b"echo\n",
        after_seq=0,
        wait_ms=100,
    )

    assert second == first
    assert factory.workers[0].writes == [b"echo\n"]
    with pytest.raises(TerminalSessionError, match="operation_id"):
        service.write(
            session_id,
            task_id=7,
            operation_id="op-1",
            data=b"different\n",
            after_seq=0,
            wait_ms=0,
        )


def test_worker_factory_failure_does_not_leave_starting_metadata(tmp_path: Path) -> None:
    service = TerminalSessionService(worker_factory=FailingWorkerFactory())

    with pytest.raises(RuntimeError, match="worker unavailable"):
        service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)


def test_process_worker_rejects_input_before_handshake() -> None:
    worker = ProcessTerminalWorker("unused", "instance")

    with pytest.raises(TerminalWorkerUnavailableError):
        worker.write(b"input")


def test_preview_disconnect_does_not_close_session(tmp_path: Path) -> None:
    service, factory = make_service()
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])
    attachment = service.subscribe(session_id, task_id=1, after_seq=None)
    service.unsubscribe(session_id, attachment.subscription)

    assert not factory.workers[0].closed
    assert service.snapshot(session_id, task_id=1)["status"] == "running"


def test_session_ownership_and_ring_buffer_gap_are_explicit(tmp_path: Path) -> None:
    service, factory = make_service(ring_buffer_bytes=4)
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])
    factory.workers[0].emit_output(b"aa")
    factory.workers[0].emit_output(b"bb")

    with pytest.raises(TerminalSessionOwnershipError):
        service.read(session_id, task_id=2, after_seq=0, wait_ms=0)
    with pytest.raises(TerminalSessionResyncRequiredError) as exc_info:
        service.read(session_id, task_id=1, after_seq=0, wait_ms=0)
    assert exc_info.value.first_available_seq == 2


def test_worker_exit_is_terminal_and_shutdown_closes_workers(tmp_path: Path) -> None:
    service, factory = make_service()
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])
    attachment = service.subscribe(session_id, task_id=1, after_seq=None)
    attachment.subscription.activate()
    factory.workers[0].emit_exit(17)

    assert service.snapshot(session_id, task_id=1)["status"] == "exited"
    assert attachment.subscription.get(0.1)["type"] == "exit"
    service.shutdown()
    assert factory.workers[0].closed


def test_session_status_observer_receives_async_exit_after_start(tmp_path: Path) -> None:
    factory = FakeWorkerFactory()
    changes = []
    service = TerminalSessionService(
        worker_factory=factory,
        status_observer=changes.append,
    )

    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=10,
    )
    factory.workers[0].emit_exit(7)

    assert [change.status for change in changes] == ["running", "exited"]
    assert changes[-1].session_id == snapshot["session_id"]
    assert changes[-1].task_id == 1
    assert snapshot["created_by_run_id"] == 10
    assert changes[-1].exit_code == 7


def test_session_status_observer_receives_failure_and_explicit_close(tmp_path: Path) -> None:
    failure_factory = FakeWorkerFactory()
    failure_changes = []
    failure_service = TerminalSessionService(
        worker_factory=failure_factory,
        status_observer=failure_changes.append,
    )
    failure_service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=11,
    )
    failure_factory.workers[0].emit_error("WORKER_FAILED")

    assert [change.status for change in failure_changes] == ["running", "failed"]

    close_factory = FakeWorkerFactory()
    close_changes = []
    close_service = TerminalSessionService(
        worker_factory=close_factory,
        status_observer=close_changes.append,
    )
    close_snapshot = close_service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=12,
    )
    close_service.close(str(close_snapshot["session_id"]), task_id=1)

    assert [change.status for change in close_changes] == ["running", "closed"]


def test_terminal_completion_is_retired_from_active_registry(tmp_path: Path) -> None:
    service, factory = make_service()
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])

    factory.workers[0].emit_exit(0)

    assert session_id not in service._registry
    assert session_id in service._terminal_history
    assert service.snapshot(session_id, task_id=1)["status"] == "exited"


def test_signal_delivery_failure_is_returned_without_false_success(tmp_path: Path) -> None:
    factory = SignalFailureWorkerFactory()
    service = TerminalSessionService(worker_factory=factory)
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


def test_terminal_history_and_write_idempotency_cache_are_bounded(tmp_path: Path) -> None:
    service, factory = make_service()
    session_ids: list[str] = []
    for index in range(Constant.Terminal.MAX_TERMINAL_HISTORY_SESSIONS + 1):
        snapshot = service.start(
            task_id=1,
            workspace_id=1,
            workspace_root=str(tmp_path),
            run_id=index + 1,
        )
        session_ids.append(str(snapshot["session_id"]))
        factory.workers[-1].emit_exit(0)

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
            data=b"x",
            wait_ms=0,
        )

    assert (
        len(service._registry[str(active["session_id"])].accepted_writes)
        == Constant.Terminal.MAX_WRITE_OPERATION_CACHE
    )
    service.shutdown()


def test_fatal_worker_error_is_not_projected_as_shell_exit(tmp_path: Path) -> None:
    service, factory = make_service()
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])
    factory.workers[0].emit_error("PTY_WRITE_FAILED")

    current = service.snapshot(session_id, task_id=1)
    assert current["status"] == "failed"
    assert current["end_reason"] == "worker_pty_write_failed"
    assert factory.workers[0].closed


def test_task_deletion_releases_worker_and_metadata_hook(tmp_path: Path) -> None:
    service, factory = make_service()
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
        run_id=1,
    )
    session_id = str(snapshot["session_id"])

    assert service.delete_task_sessions([1]) == 1
    assert factory.workers[0].closed
    with pytest.raises(TerminalSessionNotFoundError):
        service.snapshot(session_id, task_id=1)


def test_task_deletion_closes_all_runs_and_blocks_late_start(tmp_path: Path) -> None:
    service, factory = make_service()
    first = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=9)
    service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=10)
    other = service.start(task_id=2, workspace_id=1, workspace_root=str(tmp_path), run_id=11)

    assert service.delete_task_sessions([1]) == 2
    assert all(worker.closed for worker in factory.workers[:2])
    assert not factory.workers[2].closed
    with pytest.raises(TerminalSessionNotFoundError):
        service.snapshot(str(first["session_id"]), task_id=1)
    with pytest.raises(TerminalSessionStateError, match="closing"):
        service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=12)
    assert service.snapshot(str(other["session_id"]), task_id=2)["status"] == "running"
    service.shutdown()


def test_shell_resolver_has_platform_specific_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = ShellResolver()
    monkeypatch.setattr("app.service.terminal.shell_resolver.shutil.which", lambda value: value)

    windows = resolver.resolve("auto", system="Windows")
    cmd = resolver.resolve("cmd", system="Windows")
    pwsh = resolver.resolve("pwsh", system="Windows")
    posix = resolver.resolve("auto", system="Linux", environ={"SHELL": "/bin/bash"})

    assert windows.kind == "powershell"
    assert windows.argv[-1] == "-NoLogo"
    assert cmd.kind == "cmd"
    assert cmd.argv[-2:] == ("/Q", "/D")
    assert pwsh.kind == "pwsh"
    assert pwsh.argv[-1] == "-NoLogo"
    assert posix.kind == "bash"


@pytest.mark.asyncio
async def test_preview_websocket_rejects_input_after_attach(tmp_path: Path) -> None:
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

    service, factory = make_service()
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
    assert factory.workers[0].writes == []


# 本文件全部用例都需要真实后端环境（隔离主库 + 进程级单例）。
pytestmark = pytest.mark.usefixtures("backend_env")


def test_write_retry_after_output_gap_does_not_repeat_input(tmp_path: Path) -> None:
    service, factory = make_service(ring_buffer_bytes=4)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    worker = factory.workers[0]
    worker.emit_output(b"aa")
    worker.emit_output(b"bb")
    first = service.write(session_id, task_id=1, operation_id="op", data=b"x", after_seq=0)
    retry = service.write(session_id, task_id=1, operation_id="op", data=b"x", after_seq=0)
    assert first.output_gap and retry.output_gap
    assert worker.writes == [b"x"]
    assert output_texts(retry) == ["x"]
    service.shutdown()


def test_write_retry_after_read_failure_keeps_acceptance(tmp_path: Path, monkeypatch) -> None:
    service, factory = make_service()
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    original_read = service.read

    def fail_read(*args, **kwargs):
        raise RuntimeError("read failed after acceptance")

    monkeypatch.setattr(service, "read", fail_read)
    with pytest.raises(RuntimeError, match="read failed"):
        service.write(session_id, task_id=1, operation_id="op", data=b"x")
    monkeypatch.setattr(service, "read", original_read)
    service.write(session_id, task_id=1, operation_id="op", data=b"x", wait_ms=0)
    assert factory.workers[0].writes == [b"x"]
    service.shutdown()


def test_concurrent_write_retries_enqueue_once(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    service, factory = make_service()
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: service.write(session_id, task_id=1, operation_id="op", data=b"x"),
                range(16),
            )
        )
    assert all(output_texts(result) == ["x"] for result in results)
    assert factory.workers[0].writes == [b"x"]
    service.shutdown()


def test_cancel_before_acceptance_leaves_session_usable(tmp_path: Path) -> None:
    service, factory = make_service()
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    with pytest.raises(TerminalSessionStateError, match="not accepted"):
        service.write(
            session_id, task_id=1, operation_id="op", data=b"x", is_cancelled=lambda: True
        )
    assert factory.workers[0].writes == []
    service.write(session_id, task_id=1, operation_id="op", data=b"x")
    assert factory.workers[0].writes == [b"x"]
    assert not factory.workers[0].closed
    service.shutdown()


def test_cancel_after_acceptance_does_not_repeat_input(tmp_path: Path, monkeypatch) -> None:
    import threading

    service, factory = make_service()
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    session_id = str(session["session_id"])
    cancelled = threading.Event()
    worker = factory.workers[0]

    def accept_then_cancel(data):
        worker.writes.append(data)
        cancelled.set()

    monkeypatch.setattr(worker, "write", accept_then_cancel)
    result = service.write(
        session_id,
        task_id=1,
        operation_id="op",
        data=b"x",
        wait_ms=30000,
        is_cancelled=cancelled.is_set,
    )
    assert result.status == "running"
    cancelled.clear()
    service.write(session_id, task_id=1, operation_id="op", data=b"x", wait_ms=0)
    assert worker.writes == [b"x"]
    service.shutdown()


def test_list_sessions_is_task_scoped_and_keeps_recent_exits(tmp_path: Path) -> None:
    service, factory = make_service()
    first = service.start(
        task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1, label="dev server"
    )
    service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=2)
    service.start(task_id=2, workspace_id=1, workspace_root=str(tmp_path), run_id=3)
    sessions = service.list_sessions(task_id=1, workspace_id=1)
    assert [item["created_by_run_id"] for item in sessions] == [1, 2]
    assert sessions[0]["label"] == "dev server"
    assert not {"worker_pid", "worker_instance_id", "shell_executable"} & sessions[0].keys()
    assert service.list_sessions(task_id=1, workspace_id=2) == []
    factory.workers[0].emit_exit(0)
    sessions = service.list_sessions(task_id=1, workspace_id=1)
    assert (
        next(item for item in sessions if item["session_id"] == first["session_id"])["status"]
        == "exited"
    )
    service.delete_task_sessions([1])
    assert service.list_sessions(task_id=1, workspace_id=1) == []
    service.shutdown()


@pytest.mark.asyncio
async def test_workflow_finalization_preserves_task_sessions(tmp_path: Path) -> None:
    from app.core.workflows.react.workflow import ReactLikeWorkflow

    service, factory = make_service()
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    await ReactLikeWorkflow().finalize_run(1)
    discovered = service.list_sessions(task_id=1, workspace_id=1)
    assert discovered[0]["session_id"] == session["session_id"]
    service.write(str(discovered[0]["session_id"]), task_id=1, data=b"next run", wait_ms=0)
    assert factory.workers[0].writes == [b"next run"]
    service.shutdown()
    assert factory.workers[0].closed
    with pytest.raises(TerminalSessionStateError, match="shutting down"):
        service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=2)


def _queue_space(task_id: int) -> TaskRuntimeSpace:
    """队列契约不依赖数据库，显式构造只包含队列的 Task 空间。"""
    space = TaskRuntimeSpace.__new__(TaskRuntimeSpace)
    space.task_id = task_id
    space.system_queue = SimpleQueue()
    return space


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

    class SlowFactory(FakeWorkerFactory):
        def create(self, instance_id):
            worker = SlowWorker()
            self.workers.append(worker)
            return worker

    factory = SlowFactory()
    service = TerminalSessionService(worker_factory=factory)
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
    assert factory.workers[0].closed
    assert service.list_sessions(task_id=1, workspace_id=1) == []
    service.shutdown()


def test_terminal_lifecycle_writes_structured_file_log(tmp_path: Path) -> None:
    import json

    from app.config.logging.configuration import configure_logging, shutdown_logging

    log_dir = tmp_path / "logs"
    configure_logging(log_dir)
    service, _ = make_service()
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
