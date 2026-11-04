"""统一执行入口 run_background_work 与通用作业：前台 / 后台、转到后台、Run 取消与撤销。"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from app.core.tool.background import backgraound_registry as background_module
from app.core.tool.background.backgraound_registry import background_registry
from app.core.tool.background.background_job import BackgroundConclusion, BackgroundJob
from app.core.tool.background.run import run_background_work


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, dict[str, Any]]]:
    calls: list[tuple[str | None, dict[str, Any]]] = []

    def deliver(task_id, *, tool_call_id, tool_name, result, placeholder_display_data=None) -> bool:
        calls.append((tool_call_id, result))
        return True

    monkeypatch.setattr(background_module, "deliver_background_result", deliver)
    return calls


def _run(job: Any, **overrides: Any):
    options = {
        "task_id": 1,
        "run_id": 2,
        "tool_call_id": "call-1",
        "kind": "terminal_command",
        "label": "t",
        "mode": "foreground",
        "is_cancelled": lambda: False,
    }
    return run_background_work(job, **{**options, **overrides})


def _job() -> BackgroundJob:
    return BackgroundJob(
        on_cancel=lambda: None,
        cancelled_result=BackgroundConclusion({"end_reason": "cancelled"}),
    )


def _in_thread(target):
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.setdefault("outcome", target()))
    thread.start()
    return thread, box


def _wait_registered(tool_call_id: str = "call-1") -> None:
    deadline = time.monotonic() + 2
    while not background_registry.owns(1, tool_call_id):
        assert time.monotonic() < deadline, "not registered"
        time.sleep(0.01)


def test_foreground_returns_the_result_without_delivery(delivered) -> None:
    job = _job()
    thread, box = _in_thread(lambda: _run(job))
    _wait_registered()
    job.finish(BackgroundConclusion({"value": 1}))
    thread.join(2)
    assert box["outcome"].result.result == {"value": 1} and box["outcome"].placeholder is None
    assert delivered == [] and not background_registry.owns(1, "call-1")


def test_background_returns_placeholder_and_delivers_later(delivered) -> None:
    job = _job()
    outcome = _run(job, mode="background")
    assert outcome.placeholder == "background"
    assert background_registry.live_background(1) == {"call-1": outcome.work}
    assert (outcome.work.kind, outcome.work.label, outcome.work.ended_at) == (
        "terminal_command",
        "t",
        None,
    )
    job.finish(BackgroundConclusion({"value": 2}))
    assert delivered == [("call-1", {"value": 2})]


def test_result_that_arrived_before_background_start_is_still_delivered(delivered) -> None:
    job = _job()
    job.finish(BackgroundConclusion({"value": 3}))
    assert _run(job, mode="background").placeholder == "background"
    assert delivered == [("call-1", {"value": 3})]


def test_user_detach_moves_the_wait_to_background(delivered) -> None:
    job = _job()
    thread, box = _in_thread(lambda: _run(job))
    _wait_registered()
    assert background_registry.detach("call-1") == "detached"
    thread.join(2)
    assert box["outcome"].placeholder == "detached"
    job.finish(BackgroundConclusion({"value": 4}))
    assert delivered == [("call-1", {"value": 4})]


def test_cancel_after_detach_before_waiter_returns_keeps_a_background_placeholder(monkeypatch, delivered):
    from app.core.tool.background import run

    job = _job()
    entered = threading.Event()
    release = threading.Event()
    original = run._Route.wait

    def wait(route, timeout):
        entered.set()
        assert release.wait(2)
        return original(route, timeout)

    monkeypatch.setattr(run._Route, "wait", wait)
    thread, box = _in_thread(lambda: _run(job))
    try:
        assert entered.wait(2)
        assert background_registry.detach("call-1") == "detached"
        background_registry.withdraw("call-1")
    finally:
        release.set()
        thread.join(2)
    assert box["outcome"].placeholder == "detached"
    assert delivered == []  # 用户取消回执由取消入口投递，而非已撤销的作业。


def test_raised_thread_work_finishes_foreground_and_background(delivered):
    def broken(_):
        raise OSError("worker offline")

    def job():
        return BackgroundJob.start_in_thread(broken, wake=lambda: None, name="broken-worker")

    result = _run(job())
    assert result.result.result == {"end_reason": "failed", "error": "worker offline"}
    broken_job = job()
    done = threading.Event()
    broken_job.on_done(lambda _: done.set())
    assert done.wait(2)
    assert _run(broken_job, mode="background").placeholder == "background"
    assert delivered == [("call-1", {"end_reason": "failed", "error": "worker offline"})]


def test_run_cancel_cancels_the_job_and_returns_its_cancel_result(delivered) -> None:
    cancelled = threading.Event()
    job = BackgroundJob(
        on_cancel=cancelled.set,
        cancelled_result=BackgroundConclusion({"end_reason": "cancelled"}),
    )
    stop = threading.Event()
    thread, box = _in_thread(lambda: _run(job, is_cancelled=stop.is_set))
    _wait_registered()
    stop.set()
    thread.join(2)
    assert cancelled.is_set()
    assert box["outcome"].result.result == {"end_reason": "cancelled"}
    assert delivered == []


def test_user_cancel_of_a_foreground_entry_returns_the_cancel_result(delivered) -> None:
    job = _job()
    thread, box = _in_thread(lambda: _run(job))
    _wait_registered()
    background_registry.withdraw("call-1")
    thread.join(2)
    assert box["outcome"].result.result == {"end_reason": "cancelled"}


def test_cancelled_background_job_delivers_nothing(delivered) -> None:
    job = _job()
    _run(job, mode="background")
    background_registry.drop_task(1)
    assert delivered == []


def test_start_in_thread_runs_work_off_thread_and_wakes_on_cancel() -> None:
    gate = threading.Condition()
    results: list[dict[str, object]] = []

    def work(is_cancelled) -> BackgroundConclusion:
        with gate:
            while not is_cancelled():
                gate.wait(1)
        return BackgroundConclusion({"end_reason": "cancelled", "thread": threading.current_thread().name})

    def wake() -> None:
        with gate:
            gate.notify_all()

    job = BackgroundJob.start_in_thread(work, wake=wake, name="job-test")
    job.on_done(lambda conclusion: results.append(conclusion.result))
    job.cancel()
    deadline = time.monotonic() + 2
    while not results:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert results[0]["thread"] == "job-test"


def test_job_delivers_once() -> None:
    seen: list[dict[str, object]] = []
    job = _job()
    job.on_done(lambda conclusion: seen.append(conclusion.result))
    assert job.finish(BackgroundConclusion({"a": 1})) is True
    assert job.finish(BackgroundConclusion({"a": 2})) is False
    job.cancel()
    assert seen == [{"a": 1}]
