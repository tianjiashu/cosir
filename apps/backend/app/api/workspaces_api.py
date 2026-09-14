"""工作区域端点。

本模块承载 workspace 域的全部 HTTP 端点：工作区健康/列表/创建/删除，以及工作区下的
任务容器管理。
"""

import asyncio

from fastapi import Depends, HTTPException

from app.api.schemas import (
    CreateTaskRequest,
    CreateWorkspaceRequest,
    DeleteWorkspaceResponse,
    HealthResponse,
    TaskResponse,
    WorkspaceResponse,
)
from app.app import app
from app.core.runtime.runner import AgentRuntime
from app.models.errors.deletion_errors import DeletionBusyError
from app.service.depends import (
    get_runtime,
    get_task_service,
    get_workspace_service,
)
from app.service.task.workspace_service import WorkspaceService
from app.task_runtime.service.task_service import TaskService
from app.utils.datetime_utils import preview


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
        WorkspaceResponse(
            workspace_id=workspace.id,
            name=workspace.name,
            root_path=workspace.root_path,
            created_at=workspace.created_at.isoformat(),
            updated_at=workspace.updated_at.isoformat(),
        )
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
    return WorkspaceResponse(
        workspace_id=workspace.id,
        name=workspace.name,
        root_path=workspace.root_path,
        created_at=workspace.created_at.isoformat(),
        updated_at=workspace.updated_at.isoformat(),
    )


@app.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: int,
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
    except DeletionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    return DeleteWorkspaceResponse(workspace_id=workspace_id, deleted=True)


@app.get("/workspaces/{workspace_id}/tasks")
async def list_workspace_tasks(
    workspace_id: int,
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
            TaskResponse.from_record(
                task,
                context_window_total=task_service.get_context_window_total(task.id),
                fork_available=task_service.is_fork_available(task.id),
            )
            for task in task_service.list_tasks_for_workspace(workspace_id)
        ]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@app.post("/workspaces/{workspace_id}/tasks")
async def create_workspace_task(
    workspace_id: int,
    payload: CreateTaskRequest,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> TaskResponse:
    """创建工作区下的任务容器，不启动 ConversationRun。"""
    try:
        task = await asyncio.to_thread(
            workspace_service.create_task, workspace_id, preview(payload.text) or "新对话"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except DeletionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    return TaskResponse.from_record(task)
