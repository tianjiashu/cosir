"""把节点 Run 与子 Agent Run 投影为各配置节点的访问历史。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.config.configuration import get_agent_registry as get_agent_profile_registry
from app.models.conversation_run_record import ConversationRunRecord
from app.models.task_record import TaskRecord


def _resolve_agent_role(workspace: str, agent_id: str | None) -> str | None:
    """按 ``agent_id`` 从进程内 Agent profile 注册表解析角色。

    角色是配置派生的稳定事实，不再随 Run 冻结进数据库；未配置该 Agent 时返回 ``None``，由
    调用方回退到节点运行快照或节点标识。注册表未初始化属于装配错误，``RuntimeError`` 照常上浮。
    """

    if not agent_id:
        return None
    profile = get_agent_profile_registry().resolve(workspace, agent_id)
    return profile.role if profile is not None else None


def project_node_visits(
    *,
    state: AgentTeamRunState,
    configuration: dict[str, Any],
    team_status: str,
    run_records: dict[int, ConversationRunRecord],
    child_tasks_by_parent: dict[int, list[TaskRecord]],
    workspace: str,
) -> dict[str, list[dict[str, Any]]]:
    """按配置节点聚合访问记录，保持执行历史与固定节点布局分离。

    节点 Run ID 按节点自己的访问顺序保存；节点访问只投影生命周期状态，不返回业务结果或错误正文。
    未到达节点不会被伪造成访问记录；当前节点若尚未创建 Run，则投影一个无 Run ID 的进行中占位，
    让前端能在首次请求时标出入口节点。Agent 角色按 ``workspace`` + ``agent_id`` 从 profile
    注册表解析（配置派生，不读 Run 上的冻结字段）。

    子 Agent 按创建时间归入节点访问：落在某次节点 Run 创建到下一次节点 Run 创建之间的子 Task
    属于该次访问（最后一次访问不设上界）。子 Task 不记录发起它的父 Run（会被后续 Run 复用），
    所以复用的子 Task 只出现在创建它的那次访问里。
    """

    settled_visit_counts: dict[str, int] = defaultdict(int)
    for transition in state.transition_history:
        settled_visit_counts[transition.from_node_id] += 1

    visits_by_node: dict[str, list[dict[str, Any]]] = {}
    for node in configuration["nodes"]:
        node_id = node["node_id"]
        execution = state.node_executions.get(node_id)
        visits: list[dict[str, Any]] = []
        settled_visit_count = settled_visit_counts[node_id]
        run_ids = execution.conversation_run_ids if execution is not None else []
        for index, run_id in enumerate(run_ids):
            run = run_records[run_id]
            next_run = run_records[run_ids[index + 1]] if index + 1 < len(run_ids) else None
            agents = [
                {
                    "task_id": execution.task_id,
                    "title": node["name"],
                    "agent_id": run.agent_id or node["agent_id"],
                    "role": _resolve_agent_role(workspace, run.agent_id or node["agent_id"])
                    or state.node_runtime[node_id].role
                    or node["agent_id"],
                    "status": run.status,
                }
            ]
            agents.extend(
                _project_child_agent(child, run_records, workspace)
                for child in child_tasks_by_parent.get(execution.task_id, [])
                if child.created_at >= run.created_at
                and (next_run is None or child.created_at < next_run.created_at)
            )
            visits.append(
                {
                    "visit_number": index + 1,
                    "conversation_run_id": run_id,
                    "status": run.status,
                    "agents": agents,
                }
            )

        is_current = state.current_node_id == node_id
        has_unsettled_run = len(run_ids) > settled_visit_count
        if is_current and team_status == "running" and not has_unsettled_run:
            visits.append(
                {
                    "visit_number": len(run_ids) + 1,
                    "conversation_run_id": None,
                    "status": "running",
                    "agents": [
                        {
                            "task_id": execution.task_id if execution is not None else None,
                            "title": node["name"],
                            "agent_id": node["agent_id"],
                            "role": _resolve_agent_role(workspace, node["agent_id"])
                            or state.node_runtime[node_id].role
                            or node["agent_id"],
                            "status": "running",
                        }
                    ],
                }
            )
        visits_by_node[node_id] = visits

    return visits_by_node


def _project_child_agent(
    child: TaskRecord,
    run_records: dict[int, ConversationRunRecord],
    workspace: str,
) -> dict[str, Any]:
    """用子任务当前 Run 提供状态与注册表解析的角色。"""

    child_run = run_records.get(child.current_run_id) if child.current_run_id is not None else None
    return {
        "task_id": child.id,
        "title": child.title,
        "agent_id": child_run.agent_id if child_run else None,
        "role": _resolve_agent_role(workspace, child_run.agent_id) if child_run else "子 Agent",
        "status": child_run.status if child_run else "pending",
    }
