"""工具在工作线程里等待外部请求的共用方式：轮询取消信号与时限。

执行器只在调用 handler 之前检查一次取消，也不做硬超时（见 ``ToolExecutor``）。
把工作投到别处（MCP 连接池的循环、宿主工具链路）再等结果的工具，只能自己在等待期间现读取消信号、
自己守时限；这里是这件事的唯一实现，各工具不再各写一份。

两个出口各用自己的异常，不复用 ``TimeoutError``：future 里冒出来的异常原样上浮，其中也可能有
``TimeoutError``（如 MCP 建连超时），若等待自己的时限也抛它，调用方就分不清「请求可能已执行、只是没等到」
和「对端在建连阶段就超时、请求没发出」，会把后者错说成前者。
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, wait
from typing import TypeVar

from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.tool_call_cancellation_registry import should_stop_call

T = TypeVar("T")

_POLL_SECONDS = 0.1


# 停下的是已经发给对端的请求：撤销只是不再等它，对端可能已经执行了。默认取消文案「没有产生结果」
# 对在副作用之前就停下的工具成立，对这里不成立，所以使用方取消时一律用这句。
STOPPED_IN_FLIGHT_REASON = (
    "the user stopped this call while its request was in flight; the other side may already have "
    "acted on it. Check the current state before repeating it."
)


class ToolCallStopped(Exception):
    """等待期间检出 Session 停止或本次调用被单独取消；请求已撤销。"""


class CallDeadlineExceeded(Exception):
    """等待超过了调用方给的时限；请求已撤销，对端可能已经执行了。"""


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
        CallDeadlineExceeded: 超过 ``timeout_seconds``。
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
            raise CallDeadlineExceeded
