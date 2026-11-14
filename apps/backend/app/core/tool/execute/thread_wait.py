"""thread 工具在工作线程里等待外部请求的共用方式：轮询取消信号与时限。

thread 工具的执行器只在 handler 执行前检查一次取消，也不做硬超时（见 ``ToolHandlerRunner``）。
把工作投到别处（MCP 连接池的循环、宿主工具链路）再等结果的工具，只能自己在等待期间现读取消信号、
自己守时限；这里是这件事的唯一实现，各工具不再各写一份。
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, wait
from typing import TypeVar

from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.tool_call_cancellation_registry import should_stop_call

T = TypeVar("T")

_POLL_SECONDS = 0.1


class ToolCallStopped(Exception):
    """等待期间检出 Session 停止或本次调用被单独取消；请求已撤销。"""


def wait_for_call(
    future: Future[T],
    execution_context: ToolExecutionContext | None,
    timeout_seconds: float,
) -> T:
    """等待 ``future`` 完成，期间轮询取消与时限；命中任一即 ``future.cancel()`` 撤销请求。

    撤销对 ``run_coroutine_threadsafe`` 得到的 future 会取消循环上的协程，由协程自己决定如何
    通知对端（如宿主链路补发 ``$/cancel``）。``execution_context`` 为 ``None``（不经执行器的直接
    调用）时只守时限。

    异常:
        ToolCallStopped: 检出取消。
        TimeoutError: 超过 ``timeout_seconds``。
        其余: ``future`` 自身的异常原样上浮。
    """

    deadline = time.monotonic() + timeout_seconds
    while True:
        done, _ = wait([future], timeout=_POLL_SECONDS, return_when=FIRST_COMPLETED)
        if done:
            return future.result()
        if execution_context is not None and should_stop_call(
            execution_context.session_id, execution_context.tool_call_id
        ):
            future.cancel()
            raise ToolCallStopped
        if time.monotonic() >= deadline:
            future.cancel()
            raise TimeoutError
