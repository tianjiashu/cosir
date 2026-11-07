"""终端工具的执行编排：用可控执行后端覆盖前后台交接，不依赖 shell 或真实子进程。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core.tool.background import backgraound_registry as background_module
from app.core.tool.background.backgraound_registry import background_registry
from app.core.tool.background.background_job import BackgroundJob
from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.tools.tool_handler import execute_terminal as terminal_module
from app.tools.tool_handler.execute_terminal import ExecuteTerminalTool
from app.tools.tool_handler.terminal import ExecutionResult


class _Command:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None

    def execute(self, command, _cwd, _timeout, **_kwargs) -> ExecutionResult:
        self.thread = threading.current_thread()
        self.started.set()
        assert self.release.wait(5)
        if self.error is not None:
            raise self.error
        cancelled = self.stopped.is_set()
        return ExecutionResult(command, -1 if cancelled else 0, False, cancelled)

    def stop(self) -> None:
        self.stopped.set()
        self.release.set()

    def join(self) -> None:
        assert self.started.wait(2)
        assert self.thread is not None
        self.thread.join(2)
        assert not self.thread.is_alive()


@pytest.fixture
def command(monkeypatch):
    command = _Command()
    monkeypatch.setattr(terminal_module, "LocalExecutionBackend", lambda: command)
    monkeypatch.setattr(terminal_module.TerminalOutputStream, "open", lambda _: None)
    yield command
    command.stop()
    if command.thread is not None:
        command.join()
    background_registry.drop_task(1)
    cancellation_registry.consume(1)


@pytest.fixture
def pool(command):
    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            yield executor
        finally:
            command.release.set()


@pytest.fixture
def delivered(monkeypatch):
    calls = []

    def deliver(task_id, *, tool_call_id, tool_name, result, placeholder_display_data=None):
        calls.append((tool_call_id, result))
        return True

    monkeypatch.setattr(background_module, "deliver_background_result", deliver)
    return calls


def _execute(tmp_path: Path, *, background=False, call_id="call-1", tool=None):
    return (tool or ExecuteTerminalTool()).execute(
        command=call_id, background=background,
        execution_context=ToolExecutionContext(
        call_intent="test command",
            task_id=1, run_id=2, workspace_id=1, workspace_root=tmp_path,
            step_id="step-1", tool_call_id=call_id,
        ),
    )


def _wait_registered(call_id="call-1") -> None:
    deadline = time.monotonic() + 2
    while not background_registry.owns(1, call_id):
        assert time.monotonic() < deadline, "not registered"
        time.sleep(0.01)


def test_foreground_returns_result_without_delivery(tmp_path, command, pool, delivered):
    result = pool.submit(_execute, tmp_path)
    _wait_registered()
    assert not result.done()
    command.release.set()
    observation = result.result(timeout=2)
    assert observation.status == "success" and observation.background is None
    assert observation.content == "[exit_code=0]\ncall-1"
    assert delivered == [] and not background_registry.owns(1, "call-1")


def test_background_returns_placeholder_and_delivers_later(tmp_path, command, delivered):
    observation = _execute(tmp_path, background=True)
    assert observation.background is not None and not observation.suspend
    assert (observation.background.kind, observation.background.label) == (
        "terminal_command", "test command",
    )
    assert background_registry.live_background(1) == {"call-1": observation.background}
    command.release.set()
    command.join()
    assert len(delivered) == 1
    assert delivered[0][0] == "call-1" and delivered[0][1]["end_reason"] == "exited"


@pytest.mark.parametrize("background", [False, True])
def test_result_before_registration_is_not_lost(
    tmp_path, command, monkeypatch, delivered, background,
):
    # 让真实作业线程先结束，再把作业交给工具登记，确定性覆盖提前完成。
    start = BackgroundJob.start_in_thread

    def already_finished(*args, **kwargs):
        job = start(*args, **kwargs)
        command.join()
        return job

    command.release.set()
    monkeypatch.setattr(BackgroundJob, "start_in_thread", already_finished)
    observation = _execute(tmp_path, background=background)
    assert bool(observation.background) is background
    if background:
        assert len(delivered) == 1 and delivered[0][1]["end_reason"] == "exited"
    else:
        assert observation.content == "[exit_code=0]\ncall-1" and delivered == []


def test_detach_keeps_running_and_delivers_once(tmp_path, command, pool, delivered):
    result = pool.submit(_execute, tmp_path)
    _wait_registered()
    assert background_registry.detach("call-1") == "detached"
    observation = result.result(timeout=2)
    assert observation.background is not None and not observation.suspend
    assert "the user moved" in observation.content
    assert not command.stopped.is_set() and delivered == []
    command.release.set()
    command.join()
    assert len(delivered) == 1 and delivered[0][0] == "call-1"


@pytest.mark.parametrize("withdraw", [False, True], ids=["completed", "withdrawn"])
def test_detach_before_waiter_returns_keeps_placeholder(
    tmp_path, command, pool, monkeypatch, delivered, withdraw,
):
    entered, release = threading.Event(), threading.Event()
    wait_result = terminal_module._ForegroundWait.wait_result

    def paused_wait(handle, timeout):
        entered.set()
        assert release.wait(2)
        return wait_result(handle, timeout)

    monkeypatch.setattr(terminal_module._ForegroundWait, "wait_result", paused_wait)
    result = pool.submit(_execute, tmp_path)
    try:
        assert entered.wait(2)
        assert background_registry.detach("call-1") == "detached"
        if withdraw:
            background_registry.withdraw("call-1")
        else:
            command.release.set()
        command.join()
    finally:
        release.set()
    observation = result.result(timeout=2)
    assert observation.background is not None and "the user moved" in observation.content
    assert len(delivered) == (0 if withdraw else 1)


@pytest.mark.parametrize("background", [False, True])
def test_execution_exception_finishes_the_call(tmp_path, command, delivered, background):
    command.error = OSError("worker offline")
    command.release.set()
    observation = _execute(tmp_path, background=background)
    command.join()
    if background:
        assert observation.background is not None
        assert delivered == [("call-1", {"end_reason": "failed", "error": "worker offline"})]
    else:
        assert observation.status == "error" and "worker offline" in observation.content
        assert delivered == []


@pytest.mark.parametrize("stop_run", [False, True], ids=["single_cancel", "run_cancel"])
def test_foreground_cancel_stops_command_and_waits_for_result(
    tmp_path, command, pool, delivered, stop_run,
):
    result = pool.submit(_execute, tmp_path)
    _wait_registered()
    if stop_run:
        cancellation_registry.request_cancel(1, "user")
    else:
        assert background_registry.request_user_cancel(1, "call-1", driving=False)
    observation = result.result(timeout=2)
    assert command.stopped.is_set()
    assert observation.status == "cancelled" and observation.background is None
    assert delivered == []


def test_parent_stop_drops_background_result(tmp_path, command, delivered):
    _execute(tmp_path, background=True)
    dropped = background_registry.drop_task(1)
    assert dropped["call-1"]["output"] == "call-1"
    command.join()
    assert command.stopped.is_set() and delivered == []


def test_same_tool_instance_keeps_overlapping_calls_independent(
    tmp_path, command, pool, monkeypatch, delivered,
):
    tool = ExecuteTerminalTool()
    first = pool.submit(_execute, tmp_path, tool=tool)
    _wait_registered()
    assert background_registry.detach("call-1") == "detached"
    assert first.result(timeout=2).background is not None
    second = _Command()
    monkeypatch.setattr(terminal_module, "LocalExecutionBackend", lambda: second)
    result = pool.submit(_execute, tmp_path, call_id="call-2", tool=tool)
    try:
        _wait_registered("call-2")
        command.release.set()
        command.join()
        assert not result.done() and background_registry.owns(1, "call-2")
        assert len(delivered) == 1 and delivered[0][0] == "call-1"
        second.release.set()
        observation = result.result(timeout=2)
        assert observation.content == "[exit_code=0]\ncall-2"
        assert observation.background is None and len(delivered) == 1
    finally:
        second.stop()
        result.result(timeout=2)
        second.join()
