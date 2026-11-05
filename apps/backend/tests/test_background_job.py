"""通用作业的一次性交付、线程取消和纯后台登记；终端前台编排由终端测试覆盖。"""

from __future__ import annotations

import threading
import time

from app.core.tool.background import backgraound_registry as background_module
from app.core.tool.background.background_job import BackgroundConclusion, BackgroundJob
from app.core.tool.background.run import start_background_job


def _job() -> BackgroundJob:
    return BackgroundJob(
        on_cancel=lambda: None,
        cancelled_result=BackgroundConclusion({"end_reason": "cancelled"}),
    )


def test_start_in_thread_runs_work_off_thread_and_wakes_on_cancel() -> None:
    gate = threading.Condition()
    results: list[dict[str, object]] = []

    def work(is_cancelled) -> BackgroundConclusion:
        with gate:
            while not is_cancelled():
                gate.wait(1)
        return BackgroundConclusion({
            "end_reason": "cancelled", "thread": threading.current_thread().name,
        })

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


def test_background_registration_delivers_an_already_finished_job(monkeypatch) -> None:
    calls = []

    def deliver(task_id, *, tool_call_id, tool_name, result, placeholder_display_data=None):
        calls.append((task_id, tool_call_id, tool_name, result))
        return True

    monkeypatch.setattr(background_module, "deliver_background_result", deliver)
    job = _job()
    job.finish(BackgroundConclusion({"value": 3}))
    work = start_background_job(
        job, task_id=1, run_id=2, tool_call_id="call-1", kind="child_agent", label="child",
    )
    assert (work.kind, work.label, work.run_id) == ("child_agent", "child", 2)
    assert calls == [(1, "call-1", "child_agent", {"value": 3})]
