"""把 TeamRun 的持久化执行事实投影为按访问顺序排列的节点步骤。"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.models.conversation_run_record import ConversationRunRecord
from app.models.task_record import TaskRecord


def project_execution_timeline(
    *,
    state: AgentTeamRunState,
    configuration: dict[str, Any],
    team_status: str,
    run_records: dict[int, ConversationRunRecord],
    child_tasks_by_parent: dict[int, list[TaskRecord]],
) -> list[dict[str, Any]]:
    """根据转移历史与节点 Run ID 重建实际访问序列，并按父 Run 归属子 Agent。

    Team 节点串行执行，因此转移历史就是已完成访问的顺序；每个节点快照中的 Run ID
    按该节点自己的访问顺序消费。末尾未被转移引用的 Run 只能是当前尚未结算的访问，
    用它保留执行中或失败时的最后一步。配置图本身不是执行历史，不参与排序。
    """

    nodes = {node["node_id"]: node for node in configuration["nodes"]}
    next_run_index: dict[str, int] = defaultdict(int)
    visit_count: dict[str, int] = defaultdict(int)
    steps: list[dict[str, Any]] = []

    def append_step(
        node_id: str,
        run_id: int,
        business_status: str | None,
        transition_target_node_id: str | None,
    ) -> None:
        node = nodes[node_id]
        execution = state.node_executions[node_id]
        run = run_records[run_id]
        status_color = next(
            (
                item["color"]
                for item in node["statuses"]
                if item["name"] == business_status
            ),
            None,
        )
        children = [
            child
            for child in child_tasks_by_parent.get(execution.task_id, [])
            if child.parent_run_id == run_id
        ]
        child_views = []
        for child in children:
            child_run = (
                run_records.get(child.current_run_id)
                if child.current_run_id is not None
                else None
            )
            child_views.append(
                {
                    "task_id": child.id,
                    "title": child.title,
                    "status": child_run.status if child_run else "pending",
                }
            )
        visit_count[node_id] += 1
        steps.append(
            {
                "step_number": len(steps) + 1,
                "node_visit": visit_count[node_id],
                "node_id": node_id,
                "name": node["name"],
                "agent_id": node["agent_id"],
                "goal": state.node_runtime[node_id].node_goal,
                "task_id": execution.task_id,
                "conversation_run_id": run_id,
                "status": run.status,
                "business_status": business_status,
                "business_status_color": status_color,
                "transition_target_node_id": transition_target_node_id,
                "child_agents": child_views,
            }
        )

    for transition in state.transition_history:
        node_id = transition.from_node_id
        execution = state.node_executions[node_id]
        run_id = execution.conversation_run_ids[next_run_index[node_id]]
        next_run_index[node_id] += 1
        append_step(node_id, run_id, transition.status, transition.target_node_id)

    current_node_id = state.current_node_id
    if current_node_id is not None:
        execution = state.node_executions.get(current_node_id)
        if execution is not None and next_run_index[current_node_id] < len(
            execution.conversation_run_ids
        ):
            run_id = execution.conversation_run_ids[next_run_index[current_node_id]]
            output_status = _read_business_status(run_records[run_id].final_output)
            append_step(current_node_id, run_id, output_status, None)
        elif team_status == "running":
            node = nodes[current_node_id]
            visit_count[current_node_id] += 1
            steps.append(
                {
                    "step_number": len(steps) + 1,
                    "node_visit": visit_count[current_node_id],
                    "node_id": current_node_id,
                    "name": node["name"],
                    "agent_id": node["agent_id"],
                    "goal": state.node_runtime[current_node_id].node_goal,
                    "task_id": execution.task_id if execution is not None else None,
                    "conversation_run_id": None,
                    "status": "queued",
                    "business_status": None,
                    "business_status_color": None,
                    "transition_target_node_id": None,
                    "child_agents": [],
                }
            )

    return steps


def _read_business_status(final_output: str | None) -> str | None:
    """从节点输出读取可选业务状态；未提交有效 JSON 时保留为空。"""

    if not final_output:
        return None
    try:
        output = json.loads(final_output)
    except json.JSONDecodeError:
        return None
    status = output.get("status") if isinstance(output, dict) else None
    return status if isinstance(status, str) else None
