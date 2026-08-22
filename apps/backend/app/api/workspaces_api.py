"""工作区域端点。

本模块承载 workspace 域的全部 HTTP 端点：工作区健康/列表/创建/删除，以及工作区下的
任务容器管理；并包含 workspace 级状态事件流（两段式第二步）——客户端先建立
``GET /workspaces/{workspace_id}/events/stream`` 的 SSE 订阅，再触发
``POST /workspaces/{workspace_id}/events/prepare``，由后端同步执行一次 workspace 准备
（当前为 CodeGraph 索引就绪）并经 workspace 级状态事件总线把 ``preparing → ready/degraded``
实时推回。该事件通道是通用的 workspace 状态通道，后续可扩展其他 workspace 状态事件。

workspace 状态事件端点的设计约束（见 design §4.5）：
- 客户端必须先连 SSE 再触发 prepare，确保订阅先就绪、事件全部可达（bus 无缓冲/重放）。
- ``_stream_workspace_events`` 独立成模块级函数，保证帧格式/终态 break/
  finally 退订可被单元测试稳定驱动（对齐 ``turns_api._sse_frames`` 的「service 产出
  裸事件、api 层格式化帧」范式）。
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import (
    get_runtime,
    get_task_service,
    get_workspace_event_bus,
    get_workspace_event_service,
    get_workspace_service,
)
from app.api.schemas import (
    CreateTaskRequest,
    CreateWorkspaceRequest,
    DeleteWorkspaceResponse,
    HealthResponse,
    TaskResponse,
    WorkspacePrepareResponse,
    WorkspaceResponse,
)
from app.app import app
from app.config.logging.logger import log
from app.core.runtime.runner import AgentRuntime
from app.models.enums.event_type import EventType
from app.models.event.workspace_event import WorkspaceEvent
from app.service.llm.model_resolver_service import ModelNotConfiguredError
from app.service.task.task_service import TaskService
from app.service.task.workspace_service import WorkspaceService
from app.service.workspace_event.workspace_event_bus import WorkspaceEventBus
from app.service.workspace_event.workspace_event_service import WorkspaceEventService


@app.get("/health")
async def get_health(runtime: AgentRuntime = Depends(get_runtime)) -> HealthResponse:
    """后端存活探针（liveness check）。

    当前为占位实现：仅确认进程已启动并响应，不探测子系统就绪态。
    ``runtime`` 依赖已注入但本占位实现暂未使用，保留以便后续升级为真实探针。

    真实探针（TODO，尚未实现）：聚合 storage 连通性、模型配置中心
    （默认 Agent 的默认模型是否可解析 + Key 是否就位）、可选 CodeGraph
    kernel 可达性，产出不含 secret 明文的健康摘要。届时将改为注入
    ``HealthProbeService`` 并移除无用的 ``runtime`` 参数。

    参数:
        runtime: 通过依赖注入的运行时单例；当前占位实现未使用。

    返回:
        表示进程存活的 ``HealthResponse``（不含任何 secret 或子系统详情）。

    异常:
        无。

    副作用:
        无。
    """

    return HealthResponse(status="health")


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
        # 级联删除是同步 DB 操作（单 BEGIN IMMEDIATE 写锁事务），经 asyncio.to_thread
        # 移出 event loop，避免冻结其它 task 的 turn 调度。
        await asyncio.to_thread(workspace_service.delete_workspace, workspace_id)
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
    """在工作区下创建任务并同时创建其首个 pending 轮次。

    编排逻辑收口在 ``TaskService.create_task_with_initial_turn``（业务层），本端点
    只做参数透传、异常映射与响应格式化，不持有任何创建 / 运行编排。首轮次以
    ``pending`` 状态创建，运行由前端经 ``POST /turns/{turn_id}/stream`` 触发。

    参数:
        workspace_id: 来自路由的工作区标识。
        payload: 包含首条用户输入的请求体。
        task_service: 通过依赖注入的任务 service。

    返回:
        创建后的 ``TaskResponse``（含任务元数据；首轮次标识由前端经
        ``GET /tasks/{task_id}/turns`` 取得后驱动运行）。

    异常:
        HTTPException: 当工作区不存在、agent 未注册或输入非法时抛出。

    副作用:
        经业务层在存储中创建 task 与首个 pending turn。
    """

    try:
        task, _turn = task_service.create_task_with_initial_turn(
            input_text=payload.text,
            workspace_id=payload.workspace_id,
            agent_id=payload.agent_id,
            model_name=payload.model_name,
        )
    except IntegrityError as exc:
        log.error(
            "create_workspace_task failed: workspace %s not found (foreign key violation)",
            payload.workspace_id,
        )
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except ValueError as exc:
        log.warning(
            "create_workspace_task rejected: workspace=%s agent=%s reason=%s",
            payload.workspace_id,
            payload.agent_id,
            exc,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ModelNotConfiguredError as exc:
        log.warning(
            "create_workspace_task model not configured: workspace=%s agent=%s model=%s reason=%s",
            payload.workspace_id,
            payload.agent_id,
            exc.model_name,
            exc.reason,
        )
        raise HTTPException(
            status_code=422,
            detail={
                "model_name": exc.model_name,
                "reason": exc.reason,
                "guidance": exc.guidance,
            },
        ) from exc
    return TaskResponse.from_record(task)


@app.get("/workspaces/{workspace_id}/events/stream")
async def stream_workspace_events(
    workspace_id: str,
    event_bus: WorkspaceEventBus = Depends(get_workspace_event_bus),
) -> StreamingResponse:
    """以 SSE 流式返回 workspace 状态事件。

    通用 workspace 状态事件通道：当前承载创建时的准备进度（preparing/ready/degraded），
    后续可扩展其他 workspace 状态事件。客户端须先调用本端点建立订阅，再触发 prepare，
    避免错过 preparing 事件。

    参数:
        workspace_id: 来自路由的 workspace 标识。
        event_bus: workspace 级状态事件总线。

    返回:
        text/event-stream 的 StreamingResponse；终态事件（ready/degraded）后结束流。

    异常:
        无（订阅缺失时流自然结束）。

    副作用:
        注册并最终移除一条 workspace 状态事件订阅。
    """

    return StreamingResponse(
        _stream_workspace_events(event_bus, workspace_id),
        media_type="text/event-stream; charset=utf-8",
    )


#: prepare 整体超时护栏：即便进入阻塞的 index_init，也在此上限后绝不永久挂起，
#: 超时即降级返回（对应 Settings.CODEGRAPH_INDEX_INIT_TIMEOUT_SECONDS 之上的再保险）。
# 超时60秒
PREPARE_TIMEOUT_SECONDS: float = 60.0


@app.post("/workspaces/{workspace_id}/events/prepare")
async def prepare_workspace(
    workspace_id: str,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
    event_service: WorkspaceEventService | None = Depends(get_workspace_event_service),
    event_bus: WorkspaceEventBus = Depends(get_workspace_event_bus),
) -> WorkspacePrepareResponse:
    """同步触发一次 workspace 准备（prepare）。

    当前准备动作是 CodeGraph 索引就绪；该端点走通用 workspace 状态事件通道，后续可扩展
    其他 workspace 状态事件的准备动作。客户端应先连 ``/events/stream`` 建立订阅再触发本端点。

    参数:
        workspace_id: 来自路由的 workspace 标识。
        workspace_service: 工作区 service（用于取 root_path 与 404 守卫）。
        event_service: workspace 状态事件编排 service；Kernel 不可用时为 None（降级）。
        event_bus: workspace 级状态事件总线（用于 Kernel 不可用时主动发降级终态）。

    返回:
        WorkspacePrepareResponse，含就绪状态与动作摘要。

    异常:
        HTTPException: 当 workspace 不存在（404）时抛出。

    副作用:
        发布 preparing/ready/degraded 工作状态事件到总线。

    设计要点:
        Kernel 不可用时 event_service 为 None，属于设计明确的正常降级场景。此时不仅
        HTTP 返回 unavailable，还**主动 emit 一条 WORKSPACE_DEGRADED 事件**到总线——
        因为前端先连 SSE 再 POST prepare，若只靠 HTTP 响应而 SSE 订阅者未收到终态事件，
        前端会永远停在 preparing（独立审查暴露的时序 bug 的另一半）。
    """

    try:
        # 获取workspace
        ws = workspace_service.get_workspace(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    if event_service is None:
        # Kernel 不可用（设计明确要降级的正常场景）：主动发降级终态事件 + 返回 unavailable。
        event_bus.publish(
            WorkspaceEvent(
                event_type=EventType.WORKSPACE_DEGRADED,
                workspace_id=workspace_id,
                workspace_path=ws.root_path,
                payload={
                    "workspace_path": ws.root_path,
                    "state": "unavailable",
                    "degraded_reason": "workspace event kernel unavailable",
                },
            )
        )
        return WorkspacePrepareResponse(
            workspace_id=workspace_id,
            ready=False,
            state="unavailable",
            action_taken="none",
            files_changed=0,
            duration_ms=0,
            degraded_reason="workspace event kernel unavailable",
        )
    # prepare 同步阻塞（大仓库首次 init 可达数分钟），放进线程池避免卡事件循环；
    # asyncio.wait_for 作为再保险：超过上限即取消协程并降级返回，绝不永久挂起。
    try:
        readiness = await asyncio.wait_for(
            asyncio.to_thread(event_service.prepare, workspace_id, ws.root_path),
            timeout=PREPARE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        log.error(
            "workspace_event_prepare_timeout",
            extra={
                "msg": "prepare 超过总超时上限，降级返回",
                "data": {
                    "workspace_id": workspace_id,
                    "timeout_seconds": PREPARE_TIMEOUT_SECONDS,
                },
            },
        )
        event_bus.publish(
            WorkspaceEvent(
                event_type=EventType.WORKSPACE_DEGRADED,
                workspace_id=workspace_id,
                workspace_path=ws.root_path,
                payload={
                    "workspace_path": ws.root_path,
                    "state": "timeout",
                    "degraded_reason": "workspace event prepare exceeded timeout",
                },
            )
        )
        return WorkspacePrepareResponse(
            workspace_id=workspace_id,
            ready=False,
            state="timeout",
            action_taken="none",
            files_changed=0,
            duration_ms=0,
            degraded_reason="workspace event prepare exceeded timeout",
        )
    return WorkspacePrepareResponse(
        workspace_id=workspace_id,
        ready=readiness.ready,
        state=readiness.state,
        action_taken=readiness.action_taken,
        files_changed=readiness.files_changed,
        duration_ms=readiness.duration_ms,
        degraded_reason=readiness.degraded_reason,
    )


async def _stream_workspace_events(
    event_bus: WorkspaceEventBus,
    workspace_id: str,
) -> AsyncIterator[str]:
    """把 workspace 状态事件转换为 SSE 帧；终态后结束，finally 退订。

    参数:
        event_bus: workspace 级状态事件总线。
        workspace_id: 需要订阅事件的 workspace 标识。

    生成:
        SSE 格式的事件字符串（``event: <type>\\ndata: <json>\\n\\n``）。

    异常:
        不向上抛出：订阅期间异常记录并终止流（由调用方结束响应）。

    副作用:
        订阅并在 finally 中退订 workspace 状态事件。
    """

    subscription = event_bus.subscribe(workspace_id)
    try:
        async for event in subscription:
            log.info(
                "workspace_event_stream",
                extra={"msg": "received workspace event", "data": event.to_dict()},
            )
            yield f"event: {event.event_type.value}\ndata: {json.dumps(event.to_dict())}\n\n"
            if event.event_type in {EventType.WORKSPACE_READY, EventType.WORKSPACE_DEGRADED}:
                break
    finally:
        event_bus.unsubscribe(subscription)
