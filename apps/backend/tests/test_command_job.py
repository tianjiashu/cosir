"""命令作业的一次性交付、线程取消与后台登记；终端前台编排由终端测试覆盖。"""

from __future__ import annotations

import threading

from app.tools.tool_handler.terminal.command_job import CommandConclusion, CommandJob
from app.tools.tool_handler.terminal.command_results import (
    CommandResultRegistry,
    command_results,
)


def test_start_in_thread_runs_work_off_thread_and_wakes_on_cancel() -> None:
    woken = threading.Event()

    def work() -> CommandConclusion:
        assert woken.wait(2)
        return CommandConclusion({
            "end_reason": "cancelled", "thread": threading.current_thread().name,
        })

    job = CommandJob.start_in_thread(work, wake=woken.set, name="job-test")
    assert job.wait(0.05) is None
    job.cancel()
    conclusion = job.wait(2)
    assert conclusion is not None and conclusion.result["thread"] == "job-test"


def test_job_delivers_once_and_late_subscribers_get_the_conclusion() -> None:
    seen: list[dict[str, object]] = []
    gate = threading.Event()

    def work() -> CommandConclusion:
        gate.wait(2)
        return CommandConclusion({"a": 1})

    job = CommandJob.start_in_thread(work, wake=lambda: None, name="job-once")
    job.on_done(lambda conclusion: seen.append(conclusion.result))
    gate.set()
    assert job.wait(2) == CommandConclusion({"a": 1})
    job.on_done(lambda conclusion: seen.append(conclusion.result))
    assert seen == [{"a": 1}, {"a": 1}]


def test_failed_work_concludes_as_failed() -> None:
    def work() -> CommandConclusion:
        raise RuntimeError("boom")

    job = CommandJob.start_in_thread(work, wake=lambda: None, name="job-fail")
    conclusion = job.wait(2)
    assert conclusion is not None
    assert conclusion.result == {"end_reason": "failed", "error": "boom"}


def test_background_registration_delivers_an_already_finished_job(monkeypatch) -> None:
    calls = []

    def enqueue(task_id, tool_call_id, conclusion, *, starts_run=True):
        calls.append((task_id, tool_call_id, conclusion.result))
        return True

    monkeypatch.setattr(CommandResultRegistry, "_enqueue_result", staticmethod(enqueue))
    job = CommandJob.start_in_thread(
        lambda: CommandConclusion({"value": 3}), wake=lambda: None, name="job-done",
    )
    assert job.wait(2) is not None
    work = command_results.register("call-1", task_id=1, run_id=2, label="child", job=job)
    assert (work.kind, work.label, work.run_id) == ("terminal_command", "child", 2)
    assert calls == [(1, "call-1", {"value": 3})]
