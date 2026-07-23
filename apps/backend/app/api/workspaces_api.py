"""工作区域端点。"""

from fastapi import Depends, HTTPException

from app.api.app import app
from app.api.depends.dependencies import get_runtime, get_task_service, get_workspace_service
from app.api.schemas import (
    CreateTaskRequest,
    CreateWorkspaceRequest,
    DeleteWorkspaceResponse,
    HealthResponse,
    TaskResponse,
    WorkspaceResponse,
)
from app.core.runtime.runner import AgentRuntime
from app.service.task.task_service import TaskService
from app.service.task.workspace_service import WorkspaceService


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
            TaskResponse(**task.to_dict())
            for task in task_service.list_tasks_for_workspace(workspace_id)
        ]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@app.post("/workspaces/{workspace_id}/tasks")
async def create_workspace_task(
    workspace_id: str,
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
            workspace_id=workspace_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TaskResponse(**task.to_dict())
