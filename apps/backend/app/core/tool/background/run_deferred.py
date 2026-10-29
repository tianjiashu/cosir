"""延迟结果的统一执行入口：登记、前台等待、转到后台、到上限自动转后台与回投。

工具只交出一个作业（``DeferredJob``），这里决定结论去哪：

- **前台**：工具线程等作业结论，期间复查 Run 取消；拿到结论就作为本次工具调用的结果返回。转到
  后台（用户在看板 / 卡片上，或到达 ``foreground_limit_s``）与正常结束的先后，由登记表的
  ``settle_foreground`` 在锁内裁决。
- **后台 / 转到后台**：立即返回占位原因；作业的结论之后经 ``deferred_registry.complete`` 回投为
  ``background_result``，不另开等待线程。

结论路由（前台交还工具线程，还是后台回投）在本模块的一把锁里切换：作业可能在转后台的同一刻完成，
锁保证结论只走一条路。撤销（Run 停止、用户取消）经登记表调用 ``job.cancel``，作业随后以取消结论
回调；前台据此返回取消结果，后台的回投因条目已出队而被丢弃。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import cast

from app.config.logging.logger import log
from app.core.tool.background.deferred_job import DeferredJob, DeferredOutcome
from app.core.tool.background.deferred_registry import DeferredKind, deferred_registry

# 前台等待复查 Run 取消的时间片；作业结论与转后台信号都会立即唤醒，不靠轮询。
_CANCEL_CHECK_SECONDS = 0.25


def run_deferred(
    job: DeferredJob,
    *,
    task_id: int,
    run_id: int | None,
    tool_call_id: str,
    kind: DeferredKind,
    label: str,
    background: bool,
    foreground_limit_s: float | None,
    is_cancelled: Callable[[], bool],
    detachable: bool = True,
) -> DeferredOutcome:
    """以本次调用（``tool_call_id``）登记作业并按前台 / 后台取走结论；返回结论或占位原因。

    渲染（观察文本、是否挂起、``background`` 标记）交给工具。

    ``detachable`` 为 False 时前台等待不登记为可转后台（看板仍列出、仍可撤销）。同步阻塞：前台时
    阻塞到结论、转后台或 Run 取消，调用方是工具线程。
    """

    route = _Route()
    detached = threading.Event()
    deferred_registry.register(
        tool_call_id,
        task_id=task_id,
        run_id=run_id,
        kind=kind,
        label=label,
        mode="background" if background else "foreground",
        on_cancel=job.cancel,
        on_detach=detached.set if detachable and not background else None,
    )
    if background:
        route.to_background(tool_call_id)
        job.on_done(route.deliver(tool_call_id))
        return DeferredOutcome(placeholder="background")

    job.on_done(route.deliver(tool_call_id))
    deadline = None if foreground_limit_s is None else time.monotonic() + foreground_limit_s
    while True:
        if route.wait(_CANCEL_CHECK_SECONDS) or is_cancelled():
            break
        if detached.is_set():
            route.to_background(tool_call_id)
            return DeferredOutcome(placeholder="detached")
        if deadline is not None and time.monotonic() >= deadline:
            # 到上限按「转到后台」处理：同一条登记表路径，与用户转后台的竞态由登记表裁决。
            if deferred_registry.detach(tool_call_id) == "detached":
                route.to_background(tool_call_id)
                log.info(
                    "deferred_result_foreground_limit_reached",
                    extra={
                        "msg": "前台等待到达上限，自动转到后台",
                        "data": {"task_id": task_id, "tool_call_id": tool_call_id},
                    },
                )
                return DeferredOutcome(placeholder="limit")
            deadline = None
    if not route.has_result():
        # Run 被取消：撤销作业并等它以取消结论收尾，前台照常返回结果。
        job.cancel()
        route.wait(None)
    outcome = deferred_registry.settle_foreground(tool_call_id)
    if outcome == "detached":
        # 转后台恰好先于结论被裁决：结论已在手，按后台回投。
        deferred_registry.complete(tool_call_id, route.result())
        return DeferredOutcome(placeholder="detached")
    return DeferredOutcome(result=route.result())


class _Route:
    """作业结论的去向：前台交还等待中的工具线程，或后台回投；切换与结论到达在同一把锁内。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._background = False
        self._result: dict[str, object] | None = None

    def deliver(self, tool_call_id: str) -> Callable[[dict[str, object]], None]:
        def callback(result: dict[str, object]) -> None:
            with self._lock:
                self._result = result
                background = self._background
                self._arrived.set()
            if background:
                deferred_registry.complete(tool_call_id, result)

        return callback

    def to_background(self, tool_call_id: str) -> None:
        """切到后台；结论已先到时由这里补投递（否则它会停在前台、无人取走）。"""
        with self._lock:
            self._background = True
            pending = self._result if self._arrived.is_set() else None
        if pending is not None:
            deferred_registry.complete(tool_call_id, pending)

    def wait(self, timeout: float | None) -> bool:
        return self._arrived.wait(timeout)

    def has_result(self) -> bool:
        return self._arrived.is_set()

    def result(self) -> dict[str, object]:
        """调用方保证已等到结论（``wait`` 返回 True 之后）。"""
        with self._lock:
            return cast(dict[str, object], self._result)
