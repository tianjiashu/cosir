"""``wait_for_call``：完成、取消、超时三个出口。"""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

import pytest

from app.core.tool.execute.thread_wait import ToolCallStopped, wait_for_call
from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.tool_call_cancellation_registry import (
    tool_call_cancellation_registry,
)


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=3, workspace_id=1, workspace_root=Path("."), step_id="s", run_id=1, tool_call_id="c"
    )


def test_returns_result_and_reraises_future_error() -> None:
    done: Future[int] = Future()
    done.set_result(5)
    assert wait_for_call(done, _context(), 1) == 5
    failed: Future[int] = Future()
    failed.set_exception(KeyError("x"))
    with pytest.raises(KeyError):
        wait_for_call(failed, None, 1)


def test_cancel_signal_cancels_future() -> None:
    future: Future[int] = Future()
    tool_call_cancellation_registry.mark_cancelled(3, "c")
    try:
        with pytest.raises(ToolCallStopped):
            wait_for_call(future, _context(), 5)
    finally:
        tool_call_cancellation_registry.clear(3, "c")
    assert future.cancelled()


def test_deadline_cancels_future() -> None:
    future: Future[int] = Future()
    with pytest.raises(TimeoutError):
        wait_for_call(future, None, 0.2)
    assert future.cancelled()
