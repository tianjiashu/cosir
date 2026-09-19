import base64
from dataclasses import replace
from pathlib import Path

import pytest

from app.service.terminal.errors import (
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionResyncRequiredError,
    TerminalWorkerUnavailableError,
)
from app.service.terminal.shell_resolver import ShellResolver
from app.service.terminal.terminal_session_service import TerminalSessionService
from app.service.terminal.worker import ProcessTerminalWorker
from app.utils.datetime_utils import utc_now


class FakeWorker:
    pid = 4321
    capabilities = frozenset({"signal_interrupt", "signal_eof_canonical", "signal_suspend"})

    def __init__(self) -> None:
        self._callback = None
        self.writes: list[bytes] = []
        self.signals: list[str] = []
        self.closed = False

    def start(self, spec, cwd, cols, rows, on_event) -> None:
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


class FailingWorkerFactory:
    def create(self, instance_id: str):
        raise RuntimeError("worker unavailable")


class RecordingCrud:
    def __init__(self) -> None:
        self.records = {}

    def create(self, record):
        self.records[record.session_id] = record
        return record

    def update_runtime(self, session_id: str, **values):
        record = self.records[session_id]
        self.records[session_id] = replace(record, **values, updated_at=utc_now())
        return self.records[session_id]


def make_service(*, ring_buffer_bytes: int = 1024 * 1024):
    factory = FakeWorkerFactory()
    service = TerminalSessionService(
        worker_factory=factory,
        ring_buffer_bytes=ring_buffer_bytes,
    )
    return service, factory


def test_session_agent_operations_and_readers_are_cursor_based(tmp_path: Path) -> None:
    service, factory = make_service()
    snapshot = service.start(
        task_id=7,
        workspace_id=8,
        workspace_root=str(tmp_path),
        cwd=".",
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


def test_worker_factory_failure_does_not_leave_starting_metadata(tmp_path: Path) -> None:
    crud = RecordingCrud()
    service = TerminalSessionService(crud=crud, worker_factory=FailingWorkerFactory())

    with pytest.raises(RuntimeError, match="worker unavailable"):
        service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path))

    assert len(crud.records) == 1
    record = next(iter(crud.records.values()))
    assert record.status == "failed"
    assert record.end_reason == "worker_create_failed: RuntimeError"


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
    )
    session_id = str(snapshot["session_id"])
    attachment = service.subscribe(session_id, task_id=1, after_seq=None)
    attachment.subscription.activate()
    factory.workers[0].emit_exit(17)

    assert service.snapshot(session_id, task_id=1)["status"] == "exited"
    assert attachment.subscription.get(0.1)["type"] == "exit"
    service.shutdown()
    assert factory.workers[0].closed


def test_fatal_worker_error_is_not_projected_as_shell_exit(tmp_path: Path) -> None:
    service, factory = make_service()
    snapshot = service.start(
        task_id=1,
        workspace_id=1,
        workspace_root=str(tmp_path),
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
    )
    session_id = str(snapshot["session_id"])

    assert service.delete_task_sessions([1]) == 0
    assert factory.workers[0].closed
    with pytest.raises(TerminalSessionNotFoundError):
        service.snapshot(session_id, task_id=1)


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
    )
    websocket = FakeWebSocket()

    await terminal_preview_stream(websocket, 1, str(snapshot["session_id"]), service)

    assert any(item["type"] == "protocol_error" for item in websocket.sent)
    assert websocket.closed_with is not None and websocket.closed_with[0] == 1008
    assert factory.workers[0].writes == []
