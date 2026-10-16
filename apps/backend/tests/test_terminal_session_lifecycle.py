"""重构后的终态收尾与进行中操作之间的行为边界。"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_terminal_session_service import FakeWorker, make_service

pytestmark = pytest.mark.usefixtures("backend_env")


@pytest.mark.parametrize(
    "ending, status, reason, code",
    [
        ("exit", "exited", "shell_exited", 7),
        ("close", "closed", "agent_closed", None),
        ("error", "failed", "worker_heartbeat_timeout", None),
    ],
)
def test_runtime_snapshots_notifications_and_preview_keep_consistent_values(
    tmp_path, monkeypatch, ending, status, reason, code
):
    changes = []
    service, workers = make_service(monkeypatch, status_observer=changes.append)
    started = service.start(
        task_id=1, workspace_id=2, workspace_root=str(tmp_path), run_id=3, label="server"
    )
    session_id = started["session_id"]
    attachment = service.subscribe(session_id, task_id=1, after_seq=None)
    attachment.subscription.activate()
    signalled = service.signal(session_id, task_id=1, signal_name="interrupt")
    running = changes[0]
    worker = workers[0]
    if ending == "exit":
        worker.emit_exit(code)
    elif ending == "error":
        worker.emit_error("HEARTBEAT_TIMEOUT")
    else:
        service.close(session_id, task_id=1)
    try:
        ended = service.snapshot(session_id, task_id=1)
        listed = service.list_sessions(task_id=1, workspace_id=2)[0]
        replay = service.subscribe(session_id, task_id=1, after_seq=None)
        event = attachment.subscription.get(0)
        assert event == replay.terminal_event
        for value in (ended, listed, event):
            assert (value["status"], value["end_reason"], value["exit_code"]) == (
                status,
                reason,
                code,
            )
        assert (changes[-1].status, changes[-1].end_reason, changes[-1].exit_code) == (
            status,
            reason,
            code,
        )
        # 旧投影和已送出的值对象不能随 runtime 的直接赋值而变化。
        assert started["status"] == signalled["status"] == running.status == "running"
        assert started["end_reason"] is started["exit_code"] is None
        assert running.end_reason is running.exit_code is None
        assert ended["label"] == "server"
        removed = {
            "workspace_id",
            "created_by_run_id",
            "last_activity_at",
            "ended_at",
            "worker_instance_id",
            "worker_pid",
            "shell_executable",
        }
        for value in (started, signalled, ended, listed):
            assert not removed.intersection(value)
        service.unsubscribe(session_id, replay.subscription)
    finally:
        service.unsubscribe(session_id, attachment.subscription)
        service.shutdown()


def test_close_exit_and_retry_clean_worker_and_notify_only_once(tmp_path, monkeypatch):
    changes = []
    service, workers = make_service(monkeypatch, status_observer=changes.append)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    worker = workers[0]
    calls = []

    def close_with_late_exit():
        calls.append("close")
        worker.emit_exit(0)

    monkeypatch.setattr(worker, "close", close_with_late_exit)
    first = service.close(session["session_id"], task_id=1)
    second = service.close(session["session_id"], task_id=1)
    service.shutdown()
    assert first == second
    assert calls == ["close"]
    assert [change.status for change in changes] == ["running", "closed"]


def test_explicit_close_reports_cleanup_failure_but_finishes_and_does_not_retry(
    tmp_path, monkeypatch
):
    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    calls = []

    def fail_close():
        calls.append("close")
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(workers[0], "close", fail_close)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        service.close(session["session_id"], task_id=1)
    assert service.close(session["session_id"], task_id=1)["status"] == "closed"
    service.shutdown()
    assert calls == ["close"]


def test_task_deletion_after_input_acceptance_preserves_write_result(tmp_path, monkeypatch):
    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    runtime = service._registry[session["session_id"]]
    observe = runtime._observe

    def delete_then_observe(*args, **kwargs):
        service.delete_task_sessions([1])
        return observe(*args, **kwargs)

    monkeypatch.setattr(runtime, "_observe", delete_then_observe)
    result = service.write(
        session["session_id"],
        task_id=1,
        operation_id="accepted",
        input={"type": "line", "text": "yes"},
        wait_ms=0,
    )
    assert result["input"]["accepted"]
    assert result["state"]["session_status"] == "closed"
    assert workers[0].writes == [b"yes\r"]
    assert workers[0].closed
    assert service.list_sessions(task_id=1, workspace_id=1) == []
    service.shutdown()


def test_close_wakes_an_existing_read_without_waiting_for_its_deadline(monkeypatch, tmp_path):
    service, _ = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    reading = threading.Event()

    def is_cancelled():
        reading.set()
        return False

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(
            service.read,
            session["session_id"],
            task_id=1,
            cursor=session["next_cursor"],
            wait={"until": "activity", "timeout_ms": 5000},
            is_cancelled=is_cancelled,
        )
        assert reading.wait(2)
        service.close(session["session_id"], task_id=1)
        assert result.result(timeout=2)["return_reason"] == "session_ended"
    service.shutdown()


def test_failed_start_cleans_up_once(tmp_path, monkeypatch):
    calls = []

    class FailedStartWorker(FakeWorker):
        def start(self, spec, cwd, on_event):
            raise RuntimeError("start failed")

        def close(self):
            calls.append("close")

    service, _ = make_service(monkeypatch, worker_type=FailedStartWorker)
    with pytest.raises(RuntimeError, match="start failed"):
        service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    service.shutdown()
    assert calls == ["close"]


def test_terminal_notification_cannot_be_followed_by_delayed_running(tmp_path, monkeypatch):
    changes = []
    service, workers = make_service(monkeypatch, status_observer=changes.append)
    running_ready = threading.Event()
    release_running = threading.Event()
    notify = service._notify_status_changed

    def delay_running(runtime, change):
        if change.status == "running":
            running_ready.set()
            assert release_running.wait(2)
        notify(runtime, change)

    monkeypatch.setattr(service, "_notify_status_changed", delay_running)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            starting = executor.submit(
                service.start, task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1
            )
            assert running_ready.wait(2)
            workers[0].emit_exit(0)
            release_running.set()
            assert starting.result(timeout=2)["status"] == "exited"
        assert [change.status for change in changes] == ["exited"]
    finally:
        release_running.set()
        service.shutdown()


@pytest.mark.parametrize("cleanup", ["shutdown", "delete"])
@pytest.mark.parametrize("fail", [False, True])
def test_bulk_cleanup_waits_for_existing_disposal_without_closing_twice(
    tmp_path, monkeypatch, cleanup, fail
):
    service, workers = make_service(monkeypatch)
    session = service.start(task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1)
    runtime = service._registry[session["session_id"]]
    closing = threading.Event()
    release = threading.Event()
    bulk_waiting = threading.Event()
    calls = []
    wait = runtime.wait_for_disposal

    def slow_close():
        calls.append("close")
        closing.set()
        assert release.wait(2)
        if fail:
            raise RuntimeError("cleanup failed")

    def mark_wait():
        bulk_waiting.set()
        wait()

    monkeypatch.setattr(workers[0], "close", slow_close)
    monkeypatch.setattr(runtime, "wait_for_disposal", mark_wait)
    with ThreadPoolExecutor(max_workers=2) as executor:
        close = executor.submit(service.close, session["session_id"], task_id=1)
        try:
            assert closing.wait(2)
            bulk = executor.submit(
                service.shutdown
                if cleanup == "shutdown"
                else lambda: service.delete_task_sessions([1])
            )
            assert bulk_waiting.wait(2)
            assert not bulk.done()
        finally:
            release.set()
        if fail:
            with pytest.raises(RuntimeError, match="cleanup failed"):
                close.result(timeout=2)
        else:
            close.result(timeout=2)
        bulk.result(timeout=2)
    assert calls == ["close"]
    service.shutdown()
