"""工作区域端点。"""

from fastapi import Depends, HTTPException

from app.api.app import app
from app.api.dependencies import get_runtime
from app.api.schemas import CreateTaskRequest, CreateWorkspaceRequest
from app.core.runtime.runner import AgentRuntime


@app.get("/workspaces")
async def list_workspaces(runtime: AgentRuntime = Depends(get_runtime)) -> list:
    """返回已登记的工作区列表。

    参数:
        runtime: 通过依赖注入的运行时单例。

    返回:
        工作区状态字典列表。

    异常:
        无。

    副作用:
        无。
    """

    return [workspace.to_dict() for workspace in runtime.list_workspaces()]


@app.post("/workspaces")
async def create_workspace(payload: CreateWorkspaceRequest, runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """创建一个本地工作区。

    参数:
        payload: 包含 name 与 root_path 的请求体。
        runtime: 通过依赖注入的运行时单例。

    返回:
        创建后的工作区状态。

    异常:
        HTTPException: 当输入非法时抛出。

    副作用:
        在运行时存储中创建工作区。
    """

    try:
        workspace = runtime.create_workspace(payload.name, payload.root_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return workspace.to_dict()


@app.delete("/workspaces/{workspace_id}")
async def delete_workspace(workspace_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """删除工作区及其下游任务记录。

    参数:
        workspace_id: 来自路由的工作区标识。
        runtime: 通过依赖注入的运行时单例。

    返回:
        删除结果。

    异常:
        HTTPException: 当工作区不存在时抛出。

    副作用:
        级联删除工作区下的任务、轮次、事件与步骤。
    """

    try:
        runtime.delete_workspace(workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    return {"workspace_id": workspace_id, "deleted": True}


@app.get("/workspaces/{workspace_id}/tasks")
async def list_workspace_tasks(workspace_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> list:
    """返回工作区下的任务列表。

    参数:
        workspace_id: 来自路由的工作区标识。
        runtime: 通过依赖注入的运行时单例。

    返回:
        任务状态字典列表。

    异常:
        HTTPException: 当工作区不存在时抛出。

    副作用:
        无。
    """

    try:
        return [task.to_dict() for task in runtime.list_workspace_tasks(workspace_id)]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@app.post("/workspaces/{workspace_id}/tasks")
async def create_workspace_task(
    workspace_id: str,
    payload: CreateTaskRequest,
    runtime: AgentRuntime = Depends(get_runtime),
) -> dict:
    """在工作区下创建任务容器和首个 pending turn。

    参数:
        workspace_id: 来自路由的工作区标识。
        payload: 包含首条用户输入的请求体。
        runtime: 通过依赖注入的运行时单例。

    返回:
        创建后的任务状态。

    异常:
        HTTPException: 当工作区不存在或输入非法时抛出。

    副作用:
        在运行时存储中创建 task 与首个 turn。
    """

    try:
        task = runtime.create_task(
            input_text=payload.text,
            workspace_id=workspace_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return task.to_dict()
