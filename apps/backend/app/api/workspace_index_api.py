"""Workspace 索引进度 HTTP 端点。

本模块承载 workspace 创建时的索引就绪进度流（两段式第二步）：客户端先建立
``GET /workspaces/{workspace_id}/index/stream`` 的 SSE 订阅，再触发
``POST /workspaces/{workspace_id}/index/prepare``，由后端同步执行 ``ensure_ready``
并经 workspace 级事件总线把 ``preparing → ready/degraded`` 实时推回。

设计约束（见 design §4.5）：
- 客户端必须先连 SSE 再触发 prepare，确保订阅先就绪、事件全部可达（bus 无缓冲/重放）。
- ``_stream_workspace_index_events`` 独立成模块级函数，保证帧格式/终态 break/
  finally 退订可被单元测试稳定驱动（对齐 ``turns_api._sse_turn_events`` 范式）。
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.app import app
from app.api.depends.dependencies import (
    get_workspace_index_bus,
    get_workspace_index_service,
    get_workspace_service,
)
from app.api.schemas import IndexPrepareResponse
from app.models.enums.event_type import EventType
from app.service.codegraph.workspace_index_bus import WorkspaceIndexBus
from app.service.codegraph.workspace_index_service import WorkspaceIndexService
from app.service.task.workspace_service import WorkspaceService


@app.get("/workspaces/{workspace_id}/index/stream")
async def stream_workspace_index(
    workspace_id: str,
    index_bus: WorkspaceIndexBus = Depends(get_workspace_index_bus),
) -> StreamingResponse:
    """以 SSE 流式返回 workspace 索引进度事件。

    客户端须先调用本端点建立订阅，再触发 prepare，避免错过 preparing 事件。

    参数:
        workspace_id: 来自路由的 workspace 标识。
        index_bus: workspace 级索引进度事件总线。

    返回:
        text/event-stream 的 StreamingResponse；终态事件（ready/degraded）后结束流。

    异常:
        无（订阅缺失时流自然结束）。

    副作用:
        注册并最终移除一条 workspace 索引进度订阅。
    """

    return StreamingResponse(
        _stream_workspace_index_events(index_bus, workspace_id),
        media_type="text/event-stream; charset=utf-8",
    )


@app.post("/workspaces/{workspace_id}/index/prepare")
async def prepare_workspace_index(
    workspace_id: str,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    index_service: WorkspaceIndexService | None = Depends(get_workspace_index_service),
) -> IndexPrepareResponse:
    """同步触发一次 workspace 索引进度准备（ensure_ready）。

    参数:
        workspace_id: 来自路由的 workspace 标识。
        workspace_service: 工作区 service（用于取 root_path 与 404 守卫）。
        index_service: 索引准备编排 service；Kernel 不可用时为 None（降级）。

    返回:
        IndexPrepareResponse，含就绪状态与动作摘要。

    异常:
        HTTPException: 当 workspace 不存在（404）时抛出。

    副作用:
        发布 preparing/ready/degraded 进度事件到总线。
    """

    try:
        ws = workspace_service.get_workspace(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    if index_service is None:
        # Kernel 不可用（设计明确要降级的正常场景）：不阻断，返回 degraded。
        return IndexPrepareResponse(
            workspace_id=workspace_id,
            ready=False,
            state="unavailable",
            action_taken="none",
            files_changed=0,
            duration_ms=0,
            degraded_reason="codegraph kernel unavailable",
        )
    # ensure_ready 同步阻塞（大仓库首次 init 可达数分钟），放进线程池避免卡事件循环。
    readiness = await asyncio.to_thread(index_service.prepare, workspace_id, ws.root_path)
    return IndexPrepareResponse(
        workspace_id=workspace_id,
        ready=readiness.ready,
        state=readiness.state,
        action_taken=readiness.action_taken,
        files_changed=readiness.files_changed,
        duration_ms=readiness.duration_ms,
        degraded_reason=readiness.degraded_reason,
    )


async def _stream_workspace_index_events(
    index_bus: WorkspaceIndexBus,
    workspace_id: str,
) -> AsyncIterator[str]:
    """把 workspace 索引进度事件转换为 SSE 帧；终态后结束，finally 退订。

    参数:
        index_bus: workspace 级索引进度事件总线。
        workspace_id: 需要订阅进度的 workspace 标识。

    生成:
        SSE 格式的事件字符串（``event: <type>\\ndata: <json>\\n\\n``）。

    异常:
        不向上抛出：订阅期间异常记录并终止流（由调用方结束响应）。

    副作用:
        订阅并在 finally 中退订 workspace 索引进度事件。
    """

    subscription = index_bus.subscribe(workspace_id)
    try:
        async for event in subscription:
            yield f"event: {event.event_type.value}\ndata: {json.dumps(event.to_dict())}\n\n"
            if event.event_type in {EventType.WORKSPACE_READY, EventType.WORKSPACE_DEGRADED}:
                break
    finally:
        index_bus.unsubscribe(subscription)
