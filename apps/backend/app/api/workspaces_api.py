"""工作区域端点。

本模块承载 workspace 域的全部 HTTP 端点：工作区健康/列表/创建/删除，以及工作区下的
任务容器管理；并包含 workspace 创建时的索引就绪进度流（两段式第二步）——客户端先建立
``GET /workspaces/{workspace_id}/index/stream`` 的 SSE 订阅，再触发
``POST /workspaces/{workspace_id}/index/prepare``，由后端同步执行 ``prepare`` 并经
workspace 级事件总线把 ``preparing → ready/degraded`` 实时推回。

索引进度端点的设计约束（见 design §4.5）：
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
    get_runtime,
    get_task_service,
    get_workspace_index_bus,
    get_workspace_index_service,
    get_workspace_service,
)
from app.api.schemas import (
    CreateTaskRequest,
    CreateWorkspaceRequest,
    DeleteWorkspaceResponse,
    HealthResponse,
    IndexPrepareResponse,
    TaskResponse,
    WorkspaceResponse,
)
from app.config.logging.logger import log
from app.core.runtime.runner import AgentRuntime
from app.models.enums.event_type import EventType
from app.models.workspace_index_event import WorkspaceIndexEvent
from app.service.task.task_service import TaskService
from app.service.task.workspace_service import WorkspaceService
from app.service.workspace_event.workspace_index_bus import WorkspaceIndexBus
from app.service.workspace_event.workspace_index_service import WorkspaceIndexService


@app.get("/health")
async def get_health(runtime: AgentRuntime = Depends(get_runtime)) -> HealthResponse:
    """返回后端健康状态与当前模型配置摘要。

    参数:
        runtime: 通过依赖注入的运行时单例。

    返回:
        不含 secret 原文的 ``HealthResponse``。

    异常:
        无。

    副作用:
        无。
    """

    return HealthResponse(**runtime.backend_health())


@app.get("/workspaces")
async def list_workspaces(
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> list[WorkspaceResponse]:
    """返回已登记的工作区列表。

    参数:
        workspace_service: 通过依赖注入的工作区 service。

    返回:
        ``WorkspaceResponse`` 列表。

    异常:
        无。

    副作用:
        无。
    """

    return [
        WorkspaceResponse(**workspace.to_dict())
        for workspace in workspace_service.list_workspaces()
    ]


@app.post("/workspaces")
async def create_workspace(
    payload: CreateWorkspaceRequest,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceResponse:
    """创建一个本地工作区。

    参数:
        payload: 包含 name 与 root_path 的请求体。
        workspace_service: 通过依赖注入的工作区 service。

    返回:
        创建后的 ``WorkspaceResponse``。

    异常:
        HTTPException: 当输入非法时抛出。

    副作用:
        在存储中创建工作区。
    """

    try:
        workspace = workspace_service.create_workspace(payload.name, payload.root_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return WorkspaceResponse(**workspace.to_dict())


@app.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> DeleteWorkspaceResponse:
    """删除工作区及其下游任务、轮次与运行记录。

    参数:
        workspace_id: 来自路由的工作区标识。
        workspace_service: 通过依赖注入的工作区 service。

    返回:
        删除结果 ``DeleteWorkspaceResponse``。

    异常:
        HTTPException: 当工作区不存在时抛出。

    副作用:
        级联删除工作区下的任务、轮次、事件、步骤与 durable run。
    """

    try:
        workspace_service.delete_workspace(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    return DeleteWorkspaceResponse(workspace_id=workspace_id, deleted=True)


@app.get("/workspaces/{workspace_id}/tasks")
async def list_workspace_tasks(
    workspace_id: str,
    task_service: TaskService = Depends(get_task_service),
) -> list[TaskResponse]:
    """返回工作区下的任务列表。

    参数:
        workspace_id: 来自路由的工作区标识。
        task_service: 通过依赖注入的任务 service。

    返回:
        ``TaskResponse`` 列表。

    异常:
        HTTPException: 当工作区不存在时抛出。

    副作用:
        无。
    """

    try:
        return [
            TaskResponse.from_record(task)
            for task in task_service.list_tasks_for_workspace(workspace_id)
        ]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@app.post("/workspaces/{workspace_id}/tasks")
async def create_workspace_task(
    payload: CreateTaskRequest,
    task_service: TaskService = Depends(get_task_service),
) -> TaskResponse:
    """在工作区下创建任务容器和首个 pending turn。

    参数:
        workspace_id: 来自路由的工作区标识。
        payload: 包含首条用户输入的请求体。
        task_service: 通过依赖注入的任务 service。

    返回:
        创建后的 ``TaskResponse``。

    异常:
        HTTPException: 当工作区不存在或输入非法时抛出。

    副作用:
        在存储中创建 task 与首个 turn。
    """

    try:
        task = task_service.create_task(
            input_text=payload.text,
            status="pending",
            workspace_id=payload.workspace_id,
            agent_id=payload.agent_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TaskResponse.from_record(task)


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


#: prepare 整体超时护栏：即便进入阻塞的 index_init，也在此上限后绝不永久挂起，
#: 超时即降级返回（对应 Settings.CODEGRAPH_INDEX_INIT_TIMEOUT_SECONDS 之上的再保险）。
PREPARE_TIMEOUT_SECONDS: float = 660.0


@app.post("/workspaces/{workspace_id}/index/prepare")
async def prepare_workspace_index(
    workspace_id: str,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    index_service: WorkspaceIndexService | None = Depends(get_workspace_index_service),
    index_bus: WorkspaceIndexBus = Depends(get_workspace_index_bus),
) -> IndexPrepareResponse:
    """同步触发一次 workspace 索引进度准备（prepare）。

    参数:
        workspace_id: 来自路由的 workspace 标识。
        workspace_service: 工作区 service（用于取 root_path 与 404 守卫）。
        index_service: 索引准备编排 service；Kernel 不可用时为 None（降级）。
        index_bus: workspace 级索引进度事件总线（用于 Kernel 不可用时主动发降级终态）。

    返回:
        IndexPrepareResponse，含就绪状态与动作摘要。

    异常:
        HTTPException: 当 workspace 不存在（404）时抛出。

    副作用:
        发布 preparing/ready/degraded 进度事件到总线。

    设计要点:
        Kernel 不可用时 index_service 为 None，属于设计明确的正常降级场景。此时不仅
        HTTP 返回 unavailable，还**主动 emit 一条 WORKSPACE_DEGRADED 事件**到总线——
        因为前端先连 SSE 再 POST prepare，若只靠 HTTP 响应而 SSE 订阅者未收到终态事件，
        前端会永远停在 preparing（独立审查暴露的时序 bug 的另一半）。
    """

    try:
        ws = workspace_service.get_workspace(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    if index_service is None:
        # Kernel 不可用（设计明确要降级的正常场景）：主动发降级终态事件 + 返回 unavailable。
        index_bus.publish(
            WorkspaceIndexEvent(
                event_type=EventType.WORKSPACE_DEGRADED,
                workspace_id=workspace_id,
                workspace_path=ws.root_path,
                payload={
                    "workspace_path": ws.root_path,
                    "state": "unavailable",
                    "degraded_reason": "workspace_event kernel unavailable",
                },
            )
        )
        return IndexPrepareResponse(
            workspace_id=workspace_id,
            ready=False,
            state="unavailable",
            action_taken="none",
            files_changed=0,
            duration_ms=0,
            degraded_reason="workspace_event kernel unavailable",
        )
    # prepare 同步阻塞（大仓库首次 init 可达数分钟），放进线程池避免卡事件循环；
    # asyncio.wait_for 作为再保险：超过上限即取消协程并降级返回，绝不永久挂起。
    try:
        readiness = await asyncio.wait_for(
            asyncio.to_thread(index_service.prepare, workspace_id, ws.root_path),
            timeout=PREPARE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        log.error(
            "workspace_index_prepare_timeout",
            extra={
                "msg": "prepare 超过总超时上限，降级返回",
                "data": {
                    "workspace_id": workspace_id,
                    "timeout_seconds": PREPARE_TIMEOUT_SECONDS,
                },
            },
        )
        index_bus.publish(
            WorkspaceIndexEvent(
                event_type=EventType.WORKSPACE_DEGRADED,
                workspace_id=workspace_id,
                workspace_path=ws.root_path,
                payload={
                    "workspace_path": ws.root_path,
                    "state": "timeout",
                    "degraded_reason": "workspace index prepare exceeded timeout",
                },
            )
        )
        return IndexPrepareResponse(
            workspace_id=workspace_id,
            ready=False,
            state="timeout",
            action_taken="none",
            files_changed=0,
            duration_ms=0,
            degraded_reason="workspace index prepare exceeded timeout",
        )
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
