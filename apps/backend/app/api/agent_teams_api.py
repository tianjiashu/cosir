"""Agent Team 配置保存、运行确认和本地状态查询接口。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException

from app.agent_team.coordinator import get_agent_team_coordinator
from app.agent_team.registry import get_agent_team_registry
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.api.schemas.request.confirm_agent_team_request import ConfirmAgentTeamRequest
from app.api.schemas.request.reject_agent_team_request import RejectAgentTeamRequest
from app.api.schemas.request.save_agent_team_configuration_request import (
    SaveAgentTeamConfigurationRequest,
)
from app.app import app
from app.service.agent_team.agent_team_parent_run_service import (
    AgentTeamParentRunService,
)
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
    public_state.pop("runtime", None)
    active_execution = state.active_execution()
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
        "active_node": (
            active_execution.model_dump(mode="json") if active_execution is not None else None
        ),
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


@app.post("/agent-team/runs/confirm")
async def confirm_agent_team_run(
    payload: ConfirmAgentTeamRequest,
) -> dict[str, Any]:
    """提交最终配置，确认主 Run 下的待确认 TeamRun 并异步启动对应节点。"""

    runtime_loop = asyncio.get_running_loop()
    try:
        await AgentTeamParentRunService().wait_for_parent_input(payload.team_run_id)
        row = await asyncio.to_thread(
            AgentTeamRunService().confirm_and_start,
            payload.team_run_id,
            payload.configuration,
            goal=payload.goal,
            node_goals=payload.node_goals,
            runtime_loop=runtime_loop,
        )
        return _run_payload(row)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/agent-team/runs/{run_id}")
async def get_agent_team_run(run_id: int) -> dict[str, Any]:
    """读取 TeamRun 聚合状态。"""

    try:
        row = await asyncio.to_thread(AgentTeamRunService().get, run_id)
        return _run_payload(row)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Team run not found") from exc


@app.post("/agent-team/runs/{run_id}/reject")
async def reject_agent_team_run(
    run_id: int,
    payload: RejectAgentTeamRequest,
) -> dict[str, Any]:
    """提交审阅后的目标与驳回意见，让主 Agent 重新生成待确认方案。"""

    try:
        goal = payload.goal.strip()
        node_goals = {
            node_id: node_goal.strip()
            for node_id, node_goal in payload.node_goals.items()
        }
        feedback = payload.feedback.strip()
        if not goal:
            raise ValueError("Agent Team 目标不能为空")
        if not node_goals or any(not node_goal for node_goal in node_goals.values()):
            raise ValueError("每个 Agent Team 节点都必须填写子目标")
        if not feedback:
            raise ValueError("驳回意见不能为空")
        parent_run_service = AgentTeamParentRunService()
        await parent_run_service.wait_for_parent_input(run_id)
        team_run = await asyncio.to_thread(AgentTeamRunService().get, run_id)
        await parent_run_service.resume_parent_after_rejection(
            team_run,
            goal,
            node_goals,
            feedback,
        )
        return _run_payload(team_run)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
