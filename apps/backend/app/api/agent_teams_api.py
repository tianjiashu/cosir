"""Agent Team 配置保存与本地状态查询接口。

TeamRun 的「确认 / 驳回」不在本模块：用户决定走通用 human-in-the-loop 通道（Assistant
Transport 的 ``user-input-decision`` 命令 → ``wait_user`` 节点 → ``tools`` 节点重执行被批准的
调用），使每个提问工具都不需要自带端点和「等主 Run 挂起」的轮询。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import HTTPException
from fastapi import Path as PathParameter

from app.agent_team.coordinator import get_agent_team_coordinator
from app.agent_team.registry import get_agent_team_registry
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.api.schemas.request.save_agent_team_configuration_request import (
    SaveAgentTeamConfigurationRequest,
)
from app.api.configuration.agent_team_error_messages import (
    agent_team_configuration_error_message,
)
from app.app import app
from app.service.agent_team.agent_team_run_service import AgentTeamRunService
from app.service.configuration.agent_team_configuration_service import (
    get_agent_team_configuration_service,
)
from app.service.configuration.workspace_configuration_context import (
    get_workspace_configuration_context,
)
from app.service.depends import get_workspace_service

TeamIdPath = Annotated[
    str,
    PathParameter(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]


def _run_payload(row: Any) -> dict[str, Any]:
    """把 ORM TeamRun 投影为前端可读取的有限状态。"""

    state = AgentTeamRunState.model_validate(row.state_json)
    public_state = state.model_dump(mode="json")
    # 节点运行快照包含 system_prompt 和内部模型 schema，只供 coordinator 恢复执行，
    # 不属于前端 Team 进度契约，避免通过状态查询泄露内部提示词。
    public_state.pop("node_runtime", None)
    node_results = [
        execution.model_dump(mode="json")
        for execution in state.node_executions
        if execution.completed
    ]
    return {
        "id": row.id,
        "team_id": row.team_id,
        "workspace_id": row.workspace_id,
        "parent_task_id": row.parent_task_id,
        "parent_run_id": row.parent_run_id,
        "goal": row.goal_input,
        "status": row.status,
        "node_results": node_results,
        "state": public_state,
        "end_reason": row.end_reason,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
    }


@app.post("/configuration/agent-teams")
async def save_agent_team_configuration(
    payload: SaveAgentTeamConfigurationRequest,
) -> dict[str, Any]:
    """保存用户确认后的 Team 配置 JSON。"""

    try:
        if payload.scope != "system":
            raise ValueError("system endpoint only accepts system scope")
        saved = get_agent_team_configuration_service().save_confirmed(
            payload.configuration,
            scope="system",
        )
        return saved.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Team configuration not found") from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=agent_team_configuration_error_message(exc),
        ) from exc


@app.get("/configuration/agent-teams")
async def list_agent_team_configurations() -> list[dict[str, Any]]:
    """列出 system 作用域实际拥有的 Team 配置。"""

    return [
        item.model_dump(mode="json")
        for item in get_agent_team_configuration_service().list_documents(scope="system")
    ]


@app.put("/configuration/agent-teams/{team_id}")
async def update_system_agent_team_configuration(
    team_id: TeamIdPath,
    payload: SaveAgentTeamConfigurationRequest,
) -> dict[str, Any]:
    """更新 system 作用域已有 Team 配置。"""

    if payload.scope != "system":
        raise HTTPException(status_code=400, detail="system endpoint only accepts system scope")
    try:
        saved = get_agent_team_configuration_service().update_confirmed(
            team_id,
            payload.configuration,
            scope="system",
        )
        return saved.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Team configuration not found") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=agent_team_configuration_error_message(exc),
        ) from exc


@app.delete("/configuration/agent-teams/{team_id}")
async def delete_system_agent_team_configuration(team_id: TeamIdPath) -> dict[str, Any]:
    """删除 system 作用域的 Team 配置。"""

    try:
        get_agent_team_configuration_service().delete(team_id, scope="system")
        return {"team_id": team_id, "deleted": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Team configuration not found") from exc


@app.get("/workspaces/{workspace_id}/configuration/agent-teams")
async def list_workspace_agent_team_configurations(workspace_id: int) -> list[dict[str, Any]]:
    """列出 workspace 可见的 Team 配置，并保留各项的实际所有权作用域。"""

    try:
        root = get_workspace_service().get_workspace(workspace_id).root_path
        return [
            item.model_dump(mode="json")
            for item in get_agent_team_registry().list_visible(root)
        ]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@app.post("/workspaces/{workspace_id}/configuration/agent-teams")
async def save_workspace_agent_team_configuration(
    workspace_id: int,
    payload: SaveAgentTeamConfigurationRequest,
) -> dict[str, Any]:
    """创建 workspace 作用域 Team 配置。"""

    if payload.scope != "workspace":
        raise HTTPException(
            status_code=400,
            detail="workspace endpoint only accepts workspace scope",
        )
    try:
        root = get_workspace_configuration_context(workspace_id).root
        saved = get_agent_team_configuration_service().save_confirmed(
            payload.configuration,
            scope="workspace",
            workspace_root=root,
        )
        return saved.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="workspace or Team configuration not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=agent_team_configuration_error_message(exc),
        ) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/workspaces/{workspace_id}/configuration/agent-teams/{team_id}")
async def update_workspace_agent_team_configuration(
    workspace_id: int,
    team_id: TeamIdPath,
    payload: SaveAgentTeamConfigurationRequest,
) -> dict[str, Any]:
    """更新 workspace 作用域已有 Team 配置。"""

    if payload.scope != "workspace":
        raise HTTPException(
            status_code=400,
            detail="workspace endpoint only accepts workspace scope",
        )
    try:
        root = get_workspace_configuration_context(workspace_id).root
        saved = get_agent_team_configuration_service().update_confirmed(
            team_id,
            payload.configuration,
            scope="workspace",
            workspace_root=root,
        )
        return saved.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="workspace or Team configuration not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=agent_team_configuration_error_message(exc),
        ) from exc


@app.delete("/workspaces/{workspace_id}/configuration/agent-teams/{team_id}")
async def delete_workspace_agent_team_configuration(
    workspace_id: int,
    team_id: TeamIdPath,
) -> dict[str, Any]:
    """删除 workspace 自有 Team 配置，system 同名项会重新可见。"""

    try:
        root = get_workspace_configuration_context(workspace_id).root
        get_agent_team_configuration_service().delete(
            team_id,
            scope="workspace",
            workspace_root=root,
        )
        return {"workspace_id": workspace_id, "team_id": team_id, "deleted": True}
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="workspace or Team configuration not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/agent-team/runs/{run_id}")
async def get_agent_team_run(run_id: int) -> dict[str, Any]:
    """读取 TeamRun 聚合状态。"""

    try:
        row = await asyncio.to_thread(AgentTeamRunService().get, run_id)
        return _run_payload(row)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Team run not found") from exc


@app.post("/agent-team/runs/{run_id}/cancel")
async def cancel_agent_team_run(run_id: int) -> dict[str, Any]:
    """取消 TeamRun 及其当前节点。"""

    try:
        row = await asyncio.to_thread(get_agent_team_coordinator().cancel, run_id)
        return _run_payload(row)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Team run not found") from exc
