"""Task 执行状态推送（NDJSON 长连接）。

一条连接覆盖全部 Task：侧栏同时显示多个工作区，按工作区分开会让连接数随之增长。帧：

- ``full``：整表 ``{task_id: execution_status}``，连接建立时与每隔 ``_RECONCILE_SECONDS`` 各发一次，
  覆盖订阅与整表读取交错或投递失败漏掉的变化；
- ``changed``：本批变化的 Task（同一 Task 只留最新值）；
- ``heartbeat``：安静时定期发，前端据此判断连接仍活着；WebKit 也要有后续字节才交付前一段数据。

路径不放在 ``/tasks/`` 下：``/tasks/{task_id}`` 的路由会把 ``status`` 当作 task_id 匹配后
再报校验错误。
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import Depends
from fastapi.responses import StreamingResponse

from app.app import app
from app.service.depends import get_task_status_feed
from app.service.task_status.task_status_feed import TaskStatusFeed

_HEARTBEAT_SECONDS = 2.0
_RECONCILE_SECONDS = 15.0


def _line(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _statuses(statuses: dict[int, str | None]) -> dict[str, str | None]:
    return {str(task_id): status for task_id, status in statuses.items()}


@app.get("/task-status/stream")
async def task_status_stream(
    feed: TaskStatusFeed = Depends(get_task_status_feed),
) -> StreamingResponse:
    """订阅全部 Task 的执行状态；断开连接只退订，不影响任何 Run。"""

    subscription = feed.subscribe(asyncio.get_running_loop())

    async def stream() -> AsyncIterator[bytes]:
        try:
            statuses = await asyncio.to_thread(feed.statuses)
            yield _line({"type": "full", "statuses": _statuses(statuses)})
            last_full = time.monotonic()
            while True:
                try:
                    await asyncio.wait_for(subscription.changed.wait(), timeout=_HEARTBEAT_SECONDS)
                    yield _line({"type": "changed", "statuses": _statuses(subscription.take())})
                except TimeoutError:
                    if time.monotonic() - last_full >= _RECONCILE_SECONDS:
                        last_full = time.monotonic()
                        statuses = await asyncio.to_thread(feed.statuses)
                        yield _line({"type": "full", "statuses": _statuses(statuses)})
                    else:
                        yield _line({"type": "heartbeat"})
        finally:
            feed.unsubscribe(subscription)

    return StreamingResponse(
        stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"}
    )
