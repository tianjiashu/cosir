"""工作区域端点。

本模块承载 workspace 域的全部 HTTP 端点：工作区健康/列表/创建/删除，以及工作区下的
会话容器管理。
"""

import asyncio

from fastapi import Depends, HTTPException

from app.api.schemas import (
    CreateSessionRequest,
    CreateWorkspaceRequest,
    DeleteWorkspaceResponse,
    HealthResponse,
    SessionResponse,
    WorkspaceResponse,
)
from app.app import app
from app.core.user_input import capsules
from app.models.errors.deletion_errors import DeletionBusyError
from app.service.depends import (
    get_session_service,
    get_workspace_service,
)
from app.service.session.session_service import SessionService
from app.service.workspace.workspace_service import WorkspaceService
from app.utils.datetime_utils import SESSION_TITLE_LIMIT, preview


@app.get("/health")
async def get_health() -> HealthResponse:
    """后端存活探针（liveness check）。

    当前为占位实现：仅确认进程已启动并响应，不探测子系统就绪态。真实探针（TODO）将聚合
    storage 连通性与模型配置中心，产出不含 secret 明文的健康摘要。

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
    """删除工作区及其下游会话、轮次与运行记录。

    参数:
        workspace_id: 来自路由的工作区标识。
        workspace_service: 通过依赖注入的工作区 service。

    返回:
        删除结果 ``DeleteWorkspaceResponse``。

    异常:
        HTTPException: 当工作区不存在时抛出。

    副作用:
        经 ``WorkspaceService.delete_workspace_tree``：先停止工作区内的活动执行并等待释放，再级联
        删除工作区下的会话、轮次、事件、步骤与 durable run。
    """

    try:
        await workspace_service.delete_workspace_tree(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except DeletionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    return DeleteWorkspaceResponse(workspace_id=workspace_id, deleted=True)


@app.get("/workspaces/{workspace_id}/sessions")
async def list_workspace_sessions(
    workspace_id: int,
    session_service: SessionService = Depends(get_session_service),
) -> list[SessionResponse]:
    """返回工作区下的会话列表。

    参数:
        workspace_id: 来自路由的工作区标识。
        session_service: 通过依赖注入的会话 service。

    返回:
        ``SessionResponse`` 列表。

    异常:
        HTTPException: 当工作区不存在时抛出。

    副作用:
        无。
    """

    try:
        return [
            SessionResponse.from_record(
                session,
            )
            for session in session_service.list_sessions_for_workspace(workspace_id)
        ]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@app.post("/workspaces/{workspace_id}/sessions")
async def create_workspace_session(
    workspace_id: int,
    payload: CreateSessionRequest,
    workspace_service: WorkspaceService = Depends(get_workspace_service),
) -> SessionResponse:
    """创建工作区下的会话容器，不启动 Run。"""
    try:
        session = await asyncio.to_thread(
            workspace_service.create_session,
            workspace_id,
            preview(capsules.plain_text(payload.text), limit=SESSION_TITLE_LIMIT) or "新会话",
            payload.creation_command_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except DeletionBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message, "retryable": True},
        ) from exc
    return SessionResponse.from_record(session)
