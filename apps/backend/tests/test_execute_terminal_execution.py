"""终端工具的执行编排：用可控执行后端覆盖前后台交接，不依赖 shell 或真实子进程。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.tools.tool_handler.terminal import command_results as background_module
from app.tools.tool_handler.terminal.command_job import CommandJob
from app.tools.tool_handler.terminal.command_results import command_results
from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.tool_handler.terminal import execute_terminal as terminal_module
from app.tools.tool_handler.terminal.execute_terminal import ExecuteTerminalTool
from tests.live_cold_support import registered


class _Command:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: Exception | None = None
        self.timeout: float | None = None

    def execute(self, command, _cwd, timeout, output, **_kwargs) -> ExecutionResult:
        self.timeout = timeout
        self.thread = threading.current_thread()
        self.started.set()
        assert self.release.wait(5)
        if self.error is not None:
            raise self.error
        output.feed(command.encode())
        output.finish()
        if self.stopped.is_set():
            return ExecutionResult(end="cancelled")
        return ExecutionResult(end="exited", exit_code=0)

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
    monkeypatch.setattr(terminal_module.LiveOutputChannel, "open", lambda _: None)
    yield command
    command.stop()
    if command.thread is not None:
        command.join()
    command_results.drop_task(1)
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
    while not registered(command_results, 1, call_id):
        assert time.monotonic() < deadline, "not registered"
        time.sleep(0.01)


@pytest.mark.parametrize(("background", "expected"), [(True, None), (False, 300.0)])
def test_only_background_commands_run_without_a_timeout_by_default(
    tmp_path, command, pool, delivered, background, expected,
):
    """后台命令属于 Task、缺省不限时（开发服务器要常驻）；前台缺省仍是 300 秒。"""
    result = pool.submit(_execute, tmp_path, background=background)
    assert command.started.wait(2)
    assert command.timeout == expected
    if background:
        assert "no timeout" in (result.result(timeout=2).content or "")
    command.release.set()
    result.result(timeout=2)
    command.join()


def test_foreground_returns_result_without_delivery(tmp_path, command, pool, delivered):
    result = pool.submit(_execute, tmp_path)
    _wait_registered()
    assert not result.done()
    command.release.set()
    observation = result.result(timeout=2)
    assert observation.status == "success" and observation.background is None
    assert observation.content == "[exit_code=0]\ncall-1\n"
    assert delivered == [] and not registered(command_results, 1, "call-1")


def test_background_returns_placeholder_and_delivers_later(tmp_path, command, delivered):
    observation = _execute(tmp_path, background=True)
    assert observation.background is not None and not observation.suspend
    assert (observation.background.kind, observation.background.label) == (
        "terminal_command", "test command",
    )
    assert command_results.live_background(1) == {"call-1": observation.background}
    command.release.set()
    command.join()
    assert len(delivered) == 1
    assert delivered[0][0] == "call-1" and delivered[0][1]["end_reason"] == "exited"


@pytest.mark.parametrize("background", [False, True])
def test_result_before_registration_is_not_lost(
    tmp_path, command, monkeypatch, delivered, background,
):
    # 让真实作业线程先结束，再把作业交给工具登记，确定性覆盖提前完成。
    start = CommandJob.start_in_thread

    def already_finished(*args, **kwargs):
        job = start(*args, **kwargs)
        command.join()
        return job

    command.release.set()
    monkeypatch.setattr(CommandJob, "start_in_thread", already_finished)
    observation = _execute(tmp_path, background=background)
    assert bool(observation.background) is background
    if background:
        assert len(delivered) == 1 and delivered[0][1]["end_reason"] == "exited"
    else:
        assert observation.content == "[exit_code=0]\ncall-1\n" and delivered == []


def test_detach_keeps_running_and_delivers_once(tmp_path, command, pool, delivered):
    result = pool.submit(_execute, tmp_path)
    _wait_registered()
    assert command_results.detach(1, "call-1") == "detached"
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
        assert command_results.detach(1, "call-1") == "detached"
        if withdraw:
            command_results.withdraw(1, "call-1")
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
        assert command_results.request_cancel(1, "call-1", driving=False)
    observation = result.result(timeout=2)
    assert command.stopped.is_set()
    assert observation.status == "cancelled" and observation.background is None
    assert delivered == []


def test_parent_stop_drops_background_result(tmp_path, command, delivered):
    _execute(tmp_path, background=True)
    dropped = command_results.drop_task(1)
    assert dropped["call-1"]["output"] == "call-1"
    command.join()
    assert command.stopped.is_set() and delivered == []


def test_same_tool_instance_keeps_overlapping_calls_independent(
    tmp_path, command, pool, monkeypatch, delivered,
):
    tool = ExecuteTerminalTool()
    first = pool.submit(_execute, tmp_path, tool=tool)
    _wait_registered()
    assert command_results.detach(1, "call-1") == "detached"
    assert first.result(timeout=2).background is not None
    second = _Command()
    monkeypatch.setattr(terminal_module, "LocalExecutionBackend", lambda: second)
    result = pool.submit(_execute, tmp_path, call_id="call-2", tool=tool)
    try:
        _wait_registered("call-2")
        command.release.set()
        command.join()
        assert not result.done() and registered(command_results, 1, "call-2")
        assert len(delivered) == 1 and delivered[0][0] == "call-1"
        second.release.set()
        observation = result.result(timeout=2)
        assert observation.content == "[exit_code=0]\ncall-2\n"
        assert observation.background is None and len(delivered) == 1
    finally:
        second.stop()
        result.result(timeout=2)
        second.join()


class _NoisyCommand:
    """立即结束、带大量输出的后端：只为模型正文的预算口径。"""

    def __init__(self, text: str, result: ExecutionResult) -> None:
        self.text, self.result = text, result

    def execute(self, _command, _cwd, _timeout, output, **_kwargs) -> ExecutionResult:
        output.feed(self.text.encode())
        output.finish()
        return self.result

    def stop(self) -> None:
        pass


def _lines_totalling(chars: int) -> str:
    line = "x" * 39 + "\n"
    return line * (chars // len(line))


@pytest.mark.parametrize(
    "result",
    [
        ExecutionResult(end="exited", exit_code=1),
        ExecutionResult(end="timed_out"),
        ExecutionResult(end="worker_failed", failure="the terminal worker stopped unexpectedly"),
    ],
    ids=lambda result: result.end,
)
@pytest.mark.parametrize(
    "output_chars",
    [
        ExecuteTerminalTool.model_output_budget() - 40,  # 刚好不溢出
        ExecuteTerminalTool.model_output_budget() + 40,  # 刚好溢出
        200_000,  # 远超预算
    ],
)
@pytest.mark.parametrize("background", [False, True])
def test_model_text_fits_the_channel_once_whatever_the_ending(
    tmp_path, monkeypatch, delivered, result, output_chars, background
):
    """目的：执行器的通用预算对本工具恒为空操作。潜在缺陷：结局提示在裁剪之后才追加，正文越过上限，
    通用预算再截一次、切掉中段指向完整日志的标记，并另写一份截断后的 artifact。"""
    from app.config.constant import Constant
    from app.core.tool.execute.tool_output_budget import ToolOutputBudget

    text = _lines_totalling(output_chars)
    monkeypatch.setattr(
        terminal_module, "LocalExecutionBackend", lambda: _NoisyCommand(text, result)
    )
    monkeypatch.setattr(terminal_module.LiveOutputChannel, "open", lambda _: None)
    context = ToolExecutionContext(
        call_intent="noisy", task_id=1, run_id=2, workspace_id=1, workspace_root=tmp_path,
        step_id="step-1", tool_call_id="call-1",
    )
    try:
        observation = ExecuteTerminalTool().execute(
            command="noisy", background=background, execution_context=context
        )
        if background:
            deadline = time.monotonic() + 2
            while not delivered:
                assert time.monotonic() < deadline, "not delivered"
                time.sleep(0.01)
            content = str(delivered[0][1]["output"])
        else:
            content = observation.content or ""
            assert ToolOutputBudget().apply(observation, context) is observation
    finally:
        command_results.drop_task(1)

    assert len(content) <= Constant.Tools.MAX_OUTPUT_CHARS
    assert content.startswith(result.end_marker())
    artifacts = tmp_path / ".cosir" / "tool-artifacts"
    overflowed = output_chars > ExecuteTerminalTool.model_output_budget()
    if overflowed:
        [log] = list(artifacts.iterdir())  # 只有命令自己的完整日志，没有兜底写的副本
        assert log.read_text(encoding="utf-8") == text
        assert content.count("[output truncated:") == 1
        assert log.relative_to(tmp_path).as_posix() in content
    else:
        assert not artifacts.exists()
        assert text in content
    hint = terminal_module._END_HINTS.get(result.end)
    if hint is not None:
        assert content.endswith(hint)
