"""通用作业的一次性交付、线程取消和纯后台登记；终端前台编排由终端测试覆盖。"""

from __future__ import annotations

import threading
import time

from app.tools.tool_handler.terminal import command_results as background_module
from app.tools.tool_handler.terminal.command_job import CommandConclusion, CommandJob
from app.tools.tool_handler.terminal.command_results import start_command_job


def _job() -> CommandJob:
    return CommandJob(
        on_cancel=lambda: None,
        cancelled_result=CommandConclusion({"end_reason": "cancelled"}),
    )


def test_start_in_thread_runs_work_off_thread_and_wakes_on_cancel() -> None:
    gate = threading.Condition()
    results: list[dict[str, object]] = []

    def work(is_cancelled) -> CommandConclusion:
        with gate:
            while not is_cancelled():
                gate.wait(1)
        return CommandConclusion({
            "end_reason": "cancelled", "thread": threading.current_thread().name,
        })

    def wake() -> None:
        with gate:
            gate.notify_all()

    job = CommandJob.start_in_thread(work, wake=wake, name="job-test")
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
    assert job.finish(CommandConclusion({"a": 1})) is True
    assert job.finish(CommandConclusion({"a": 2})) is False
    job.cancel()
    assert seen == [{"a": 1}]


def test_background_registration_delivers_an_already_finished_job(monkeypatch) -> None:
    calls = []

    def deliver(task_id, *, tool_call_id, tool_name, result, placeholder_display_data=None):
        calls.append((task_id, tool_call_id, tool_name, result))
        return True

    monkeypatch.setattr(background_module, "deliver_background_result", deliver)
    job = _job()
    job.finish(CommandConclusion({"value": 3}))
    work = start_command_job(
        job, task_id=1, run_id=2, tool_call_id="call-1", kind="terminal_command", label="child",
    )
    assert (work.kind, work.label, work.run_id) == ("terminal_command", "child", 2)
    assert calls == [(1, "call-1", "terminal_command", {"value": 3})]
