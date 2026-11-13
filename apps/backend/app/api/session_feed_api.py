"""Session 推送（NDJSON 长连接）：Session 状态与侧栏目录失效。

一条连接覆盖全部 Session 与工作区：侧栏同时显示多个工作区，按工作区分开会让连接数随之增长（本机
HTTP/1.1 每个主机只有 6 条连接）。每个 Session 的状态值是 ``{executionStatus, awaitingUserCalls}``
（执行状态与等你操作的调用数，见 ``SessionStatusQueries``）。帧：

- ``full``：状态整表 ``{session_id: 值}``，连接建立时与每隔 ``_RECONCILE_SECONDS`` 各发一次，
  覆盖订阅与整表读取交错或投递失败漏掉的变化；
- ``changed``：本批状态变化的 Session（同一 Session 只留最新值）；
- ``catalog``：目录失效（见 ``SessionFeed``）：``workspaces`` 为 true 表示工作区列表变了，
  ``workspaceIds`` 是侧栏 Session 有变化的工作区；前端据此重读目录接口。断开期间漏掉的失效由前端
  重连后整体重读目录收敛，所以不进周期对账；
- ``heartbeat``：安静时定期发，前端据此判断连接仍活着；WebKit 也要有后续字节才交付前一段数据。

路径不放在 ``/sessions/`` 下：``/sessions/{session_id}`` 的路由会把 ``session-feed`` 当作 session_id 匹配后
再报校验错误。
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import Depends
from fastapi.responses import StreamingResponse

from app.app import app
from app.service.depends import get_session_feed
from app.service.session_feed.session_feed import CatalogChange, SessionFeed, SessionStatuses

_HEARTBEAT_SECONDS = 2.0
_RECONCILE_SECONDS = 15.0


def _line(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _sessions(statuses: SessionStatuses) -> dict[str, dict[str, object]]:
    return {
        str(session_id): {
            "executionStatus": status.execution_status,
            "awaitingUserCalls": status.awaiting_user_calls,
        }
        for session_id, status in statuses.items()
    }


def _catalog(change: CatalogChange) -> dict[str, object]:
    return {
        "type": "catalog",
        "workspaces": change.workspaces,
        "workspaceIds": sorted(change.workspace_ids),
    }


@app.get("/session-feed/stream")
async def session_feed_stream(
    feed: SessionFeed = Depends(get_session_feed),
) -> StreamingResponse:
    """订阅全部 Session 的状态与目录失效；断开连接只退订，不影响任何 Run。"""

    subscription = feed.subscribe(asyncio.get_running_loop())

    async def stream() -> AsyncIterator[bytes]:
        try:
            statuses = await asyncio.to_thread(feed.statuses)
            yield _line({"type": "full", "sessions": _sessions(statuses)})
            last_full = time.monotonic()
            while True:
                try:
                    await asyncio.wait_for(subscription.changed.wait(), timeout=_HEARTBEAT_SECONDS)
                    batch = subscription.take()
                    if batch.statuses:
                        yield _line({"type": "changed", "sessions": _sessions(batch.statuses)})
                    if batch.catalog:
                        yield _line(_catalog(batch.catalog))
                except TimeoutError:
                    if time.monotonic() - last_full >= _RECONCILE_SECONDS:
                        last_full = time.monotonic()
                        statuses = await asyncio.to_thread(feed.statuses)
                        yield _line({"type": "full", "sessions": _sessions(statuses)})
                    else:
                        yield _line({"type": "heartbeat"})
        finally:
            feed.unsubscribe(subscription)

    return StreamingResponse(
        stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"}
    )
