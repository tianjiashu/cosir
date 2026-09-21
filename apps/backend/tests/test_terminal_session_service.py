import base64
from pathlib import Path
from typing import cast

import pytest
from langchain_core.messages import SystemMessage

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
    assert result.next_seq == 2
    assert attachment.subscription.get(0.1) == result.output[0]
    assert service.read(session_id, task_id=7, after_seq=0, wait_ms=0).output == result.output


def test_cursor_must_be_the_last_applied_seq_not_next_seq(tmp_path: Path) -> None:
    """续读必须回传「已应用的最后一个 seq」；回传 next_seq 会静默跳掉一帧。"""

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
    assert first.next_seq == 3

    worker.emit_output(b"line-2")
    worker.emit_output(b"\r\n")

    applied = service.read(session_id, task_id=7, after_seq=first.next_seq - 1, wait_ms=0)
    assert output_texts(applied) == ["line-2", "\r\n"]
    assert applied.next_seq == 5

    worker.emit_output(b"line-3")
    worker.emit_output(b"\r\n")

    # 回传 next_seq 把 seq == after_seq 的内容帧过滤掉，只留下换行帧。
    skipped = service.read(session_id, task_id=7, after_seq=applied.next_seq, wait_ms=0)
    assert output_texts(skipped) == ["\r\n"]


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


def test_input_backpressure_defers_one_task_system_message(tmp_path: Path) -> None:
    task_id = 987654
    task_space = task_runtime_spaces.get_or_create(task_id)
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

    assert len(first_messages) == 1
    assert "terminal input queue is temporarily full" in str(first_messages[0].content)
    assert first_messages[0].additional_kwargs["run_id"] == 1
    assert task_space.take_deferred_system_messages() == []
    service.shutdown()


def test_deferred_system_messages_drop_stale_run_scoped_messages() -> None:
    task_space = task_runtime_spaces.get_or_create(987655)
    task_space.take_deferred_system_messages()
    task_space.defer_system_message(
        SystemMessage(content="old", additional_kwargs={"run_id": 1})
    )
    task_space.defer_system_message(
        SystemMessage(content="current", additional_kwargs={"run_id": 2})
    )

    messages = task_space.take_deferred_system_messages(run_id=2)

    assert [message.content for message in messages] == ["current"]
    assert task_space.take_deferred_system_messages() == []


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
    service, factory = make_service(ring_buffer_bytes=2)
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

    assert len(service._write_operations) == Constant.Terminal.MAX_WRITE_OPERATION_CACHE
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


def test_run_cleanup_closes_all_owned_workers_and_blocks_late_start(tmp_path: Path) -> None:
    service, factory = make_service()
    first = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=9)
    _second = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=9)

    assert service.close_run_terminals(9, reason="run_failed") == 2
    assert all(worker.closed for worker in factory.workers)
    with pytest.raises(TerminalSessionNotFoundError):
        service.snapshot(str(first["session_id"]), task_id=1)
    with pytest.raises(TerminalSessionStateError, match="closing"):
        service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=9)
    service.begin_run(9)
    restarted = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=9)
    assert restarted["status"] == "running"


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
