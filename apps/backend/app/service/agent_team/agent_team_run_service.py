"""Agent Team 运行的读取服务：供 HTTP API 与审批决定入队边界读取 TeamRun 及其前端读模型。

审批门上的状态迁移（创建待确认、确认、驳回）只由 ``agent_team`` 工具发起，归
``app.core.tool.handler.agent_team.agent_team_run_approval``；节点调度归 Coordinator。本服务只读。
"""

from __future__ import annotations

from typing import Any

from app.models.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.service.agent_team.agent_team_execution_timeline import (
    project_node_visits,
)
from app.service.depends import get_workspace_service
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory


class AgentTeamRunService:
    """只读 TeamRun 事实并组装前端读模型。"""

    def __init__(self) -> None:
        self._team_run_crud = AgentTeamRunCrud()
        self._session_factory = main_session_factory()

    def get(self, run_id: int) -> AgentTeamRunModel:
        """读取 TeamRun。

        未找到时由 CRUD 直接抛出 ``KeyError``，交由 API 层统一映射为 404；本方法不吞咽
        异常、不返回 ``None``，避免「未找到」契约在链路中转。
        """

        with self._session_factory() as session:
            return self._team_run_crud.get_by_id(run_id, session=session)

    def get_view(self, run_id: int) -> dict[str, Any]:
        """从 TeamRun、节点 Task 与 ConversationRun 事实组装前端有限读模型。"""

        row = self.get(run_id)
        state = AgentTeamRunState.model_validate(row.state_json)
        configuration = row.configuration_snapshot_json
        tasks = TaskCrud()
        runs = ConversationRunCrud()
        executions = state.node_executions
        task_ids = [execution.task_id for execution in executions.values()]
        child_tasks_by_parent = tasks.list_by_parent_task_ids(task_ids)
        run_ids = [
            run_id for execution in executions.values() for run_id in execution.conversation_run_ids
        ]
        run_ids.extend(
            task.current_run_id
            for children in child_tasks_by_parent.values()
            for task in children
            if task.current_run_id is not None
        )
        run_records = runs.get_many(list(dict.fromkeys(run_ids)))
        workspace_root = get_workspace_service().get_workspace(row.workspace_id).root_path
        visits_by_node = project_node_visits(
            state=state,
            configuration=configuration,
            team_status=row.status,
            run_records=run_records,
            child_tasks_by_parent=child_tasks_by_parent,
            workspace=workspace_root,
        )
        node_failure_reasons = {
            AgentTeamRunEndReason.NODE_RUN_FAILED.value,
            AgentTeamRunEndReason.NODE_RUN_CANCELLED.value,
            AgentTeamRunEndReason.NODE_OUTPUT_INVALID.value,
            AgentTeamRunEndReason.TRANSITION_NOT_FOUND.value,
            AgentTeamRunEndReason.NEXT_NODE_START_FAILED.value,
            AgentTeamRunEndReason.IMPLICIT_COMPLETION_MISSING.value,
        }
        failure_node_id = (
            state.current_node_id
            if row.status == AgentTeamRunStatus.FAILED.value
            and row.end_reason in node_failure_reasons
            else None
        )
        node_views: list[dict[str, Any]] = []
        for node in configuration["nodes"]:
            node_id = node["node_id"]
            visits = visits_by_node[node_id]
            if failure_node_id == node_id:
                node_status = "failed"
            elif (
                state.current_node_id == node_id
                and row.status == AgentTeamRunStatus.RUNNING.value
                and (not visits or visits[-1]["conversation_run_id"] is None)
            ):
                node_status = "running"
            elif visits:
                node_status = visits[-1]["status"]
            elif row.status in {
                AgentTeamRunStatus.COMPLETED.value,
                AgentTeamRunStatus.FAILED.value,
                AgentTeamRunStatus.CANCELLED.value,
            }:
                node_status = "skipped"
            else:
                node_status = "queued"
            node_views.append(
                {
                    "node_id": node_id,
                    "name": node["name"],
                    "agent_id": node["agent_id"],
                    "role": state.node_runtime[node_id].role or node["agent_id"],
                    "goal_summary": state.node_runtime[node_id].node_goal_summary,
                    "status": node_status,
                    "visits": visits,
                }
            )
        return {
            "id": row.id,
            "team_id": row.team_id,
            "goal": row.goal_input,
            "status": row.status,
            "end_reason": row.end_reason,
            "nodes": node_views,
            "current_node_id": (
                state.current_node_id if row.status == AgentTeamRunStatus.RUNNING.value else None
            ),
        }
