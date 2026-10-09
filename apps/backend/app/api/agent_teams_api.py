"""Agent Team 配置保存与本地状态查询接口。

TeamRun 的「确认 / 驳回」不在本模块：用户决定走通用 human-in-the-loop 通道（Assistant
Transport 的 ``user-input-decision`` 命令 → ``wait_user`` 节点 → ``tools`` 节点重执行被批准的
调用），使每个提问工具都不需要自带端点和「等主 Run 挂起」的轮询。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException

from app.agent_team.coordinator import get_agent_team_coordinator
from app.agent_team.registry import get_agent_team_registry
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.api.schemas.request.save_agent_team_configuration_request import (
    SaveAgentTeamConfigurationRequest,
)
from app.app import app
from app.service.agent_team.agent_team_run_service import AgentTeamRunService
from app.service.configuration.agent_team_configuration_service import (
    get_agent_team_configuration_service,
)
from app.service.depends import get_workspace_service


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
        workspace_root: str | None = None
        if payload.scope == "workspace":
            if payload.workspace_id is None:
                raise ValueError("workspace_id is required for workspace scope")
            workspace_root = get_workspace_service().get_workspace(payload.workspace_id).root_path
        saved = get_agent_team_configuration_service().save_confirmed(
            payload.configuration,
            scope=payload.scope,
            workspace_root=workspace_root,
        )
        return saved.model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/configuration/agent-teams")
async def list_agent_team_configurations(workspace_id: int) -> list[dict[str, Any]]:
    """返回 workspace 可见的 Team 配置摘要。"""

    try:
        root = get_workspace_service().get_workspace(workspace_id).root_path
        return [
            item.model_dump(mode="json")
            for item in get_agent_team_registry().list_visible(root)
        ]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


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
