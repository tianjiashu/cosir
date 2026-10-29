"""统一执行入口 run_deferred 与通用作业：前台 / 后台、转到后台、到上限、Run 取消与撤销。"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from app.core.tool.background.deferred_job import DeferredConclusion
from app.core.tool.background import deferred_registry as deferred_module
from app.core.tool.background.deferred_job import ManualJob, ThreadJob
from app.core.tool.background.deferred_registry import deferred_registry
from app.core.tool.background.run_deferred import run_deferred
from app.core.tool.schemas import DeferredCapability, ToolDefinition
from app.core.tool.schemas.tool_deferred import background_field


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, dict[str, Any]]]:
    calls: list[tuple[str | None, dict[str, Any]]] = []

    def deliver(task_id, *, tool_call_id, tool_name, result, placeholder_display_data=None) -> bool:
        calls.append((tool_call_id, result))
        return True

    monkeypatch.setattr(deferred_module, "deliver_background_result", deliver)
    return calls


def _run(job: Any, **overrides: Any):
    options = {
        "task_id": 1,
        "run_id": 2,
        "tool_call_id": "call-1",
        "kind": "terminal_command",
        "label": "terminal_command:t",
        "background": False,
        "foreground_limit_s": None,
        "is_cancelled": lambda: False,
    }
    return run_deferred(job, **{**options, **overrides})


def _job() -> ManualJob:
    return ManualJob(on_cancel=lambda: None, cancelled_result=DeferredConclusion({"end_reason": "cancelled"}))


def _in_thread(target):
    box: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.setdefault("outcome", target()))
    thread.start()
    return thread, box


def _wait_registered(tool_call_id: str = "call-1") -> None:
    deadline = time.monotonic() + 2
    while not deferred_registry.owns(1, tool_call_id):
        assert time.monotonic() < deadline, "not registered"
        time.sleep(0.01)


def test_foreground_returns_the_result_without_delivery(delivered) -> None:
    job = _job()
    thread, box = _in_thread(lambda: _run(job))
    _wait_registered()
    job.finish(DeferredConclusion({"value": 1}))
    thread.join(2)
    assert box["outcome"].result.result == {"value": 1} and box["outcome"].placeholder is None
    assert delivered == [] and not deferred_registry.owns(1, "call-1")


def test_background_returns_placeholder_and_delivers_later(delivered) -> None:
    job = _job()
    outcome = _run(job, background=True)
    assert outcome.placeholder == "background"
    assert "call-1" in [view.tool_call_id for view in deferred_registry.entries(1)]
    job.finish(DeferredConclusion({"value": 2}))
    assert delivered == [("call-1", {"value": 2})]


def test_result_that_arrived_before_background_start_is_still_delivered(delivered) -> None:
    job = _job()
    job.finish(DeferredConclusion({"value": 3}))
    assert _run(job, background=True).placeholder == "background"
    assert delivered == [("call-1", {"value": 3})]


def test_user_detach_moves_the_wait_to_background(delivered) -> None:
    job = _job()
    thread, box = _in_thread(lambda: _run(job))
    _wait_registered()
    assert deferred_registry.detach("call-1") == "detached"
    thread.join(2)
    assert box["outcome"].placeholder == "detached"
    job.finish(DeferredConclusion({"value": 4}))
    assert delivered == [("call-1", {"value": 4})]


def test_foreground_limit_detaches_automatically(delivered) -> None:
    job = _job()
    outcome = _run(job, foreground_limit_s=0.05)
    assert outcome.placeholder == "limit"
    job.finish(DeferredConclusion({"value": 5}))
    assert delivered == [("call-1", {"value": 5})]


def test_non_detachable_foreground_cannot_be_moved(delivered) -> None:
    job = _job()
    thread, box = _in_thread(lambda: _run(job, detachable=False))
    _wait_registered()
    assert deferred_registry.detach("call-1") == "not_foreground"
    job.finish(DeferredConclusion({"value": 6}))
    thread.join(2)
    assert box["outcome"].result.result == {"value": 6}


def test_run_cancel_cancels_the_job_and_returns_its_cancel_result(delivered) -> None:
    cancelled = threading.Event()
    job = ManualJob(on_cancel=cancelled.set, cancelled_result=DeferredConclusion({"end_reason": "cancelled"}))
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
    deferred_registry.cancel("call-1")
    thread.join(2)
    assert box["outcome"].result.result == {"end_reason": "cancelled"}


def test_cancelled_background_job_delivers_nothing(delivered) -> None:
    job = _job()
    _run(job, background=True)
    deferred_registry.drop_task(1)
    assert delivered == []


def test_thread_job_runs_work_off_thread_and_wakes_on_cancel() -> None:
    gate = threading.Condition()
    results: list[dict[str, object]] = []

    def work(is_cancelled) -> DeferredConclusion:
        with gate:
            while not is_cancelled():
                gate.wait(1)
        return DeferredConclusion({"end_reason": "cancelled", "thread": threading.current_thread().name})

    def wake() -> None:
        with gate:
            gate.notify_all()

    job = ThreadJob(work, wake=wake, name="job-test")
    job.on_done(lambda conclusion: results.append(conclusion.result))
    job.cancel()
    deadline = time.monotonic() + 2
    while not results:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert results[0]["thread"] == "job-test"


def test_manual_job_delivers_once() -> None:
    seen: list[dict[str, object]] = []
    job = _job()
    job.on_done(lambda conclusion: seen.append(conclusion.result))
    assert job.finish(DeferredConclusion({"a": 1})) is True
    assert job.finish(DeferredConclusion({"a": 2})) is False
    job.cancel()
    assert seen == [{"a": 1}]


def test_deferred_tools_must_declare_the_background_argument() -> None:
    from pydantic import BaseModel

    capability = DeferredCapability(default_background=False, foreground_limit_s=None)

    class Plain(BaseModel):
        x: int

    class WithBackground(BaseModel):
        x: int
        background: bool | None = background_field(capability)

    def definition(model):
        return ToolDefinition(
            name="probe", group="g", description="d", handler=lambda **_: None,
            args_model=model, deferred=capability,
        )

    with pytest.raises(ValueError):
        definition(Plain).normalized()
    schema = definition(WithBackground).normalized().parameters_schema
    assert "background" in schema["properties"] and "background" in schema["required"]
