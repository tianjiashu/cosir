"""把 TeamRun 的持久化事实投影为面向 Agent 的 workspace 运行档案。"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.config.logging.logger import log
from app.core.tools.tool_handler.patch_write.atomic_write import atomic_write_text
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.models.conversation_run_failure import run_failure_message
from app.models.conversation_run_record import ConversationRunRecord
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.depends import get_workspace_service
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.utils.path.workspace_cosir import workspace_agent_team_run_dir

_TERMINAL_STATUSES = {
    AgentTeamRunStatus.COMPLETED.value,
    AgentTeamRunStatus.FAILED.value,
    AgentTeamRunStatus.CANCELLED.value,
}
_NODE_FAILURE_REASONS = {
    AgentTeamRunEndReason.NODE_RUN_FAILED.value,
    AgentTeamRunEndReason.NODE_RUN_CANCELLED.value,
    AgentTeamRunEndReason.NODE_OUTPUT_INVALID.value,
    AgentTeamRunEndReason.TRANSITION_NOT_FOUND.value,
    AgentTeamRunEndReason.NEXT_NODE_START_FAILED.value,
    AgentTeamRunEndReason.IMPLICIT_COMPLETION_MISSING.value,
}


class AgentTeamRunArchiveService:
    """生成并原子写入单次 TeamRun 的 Agent 可读 JSON 档案。

    档案只投影 Team、目标、节点输出与流转结果；运行和任务标识、错误码等开发者字段不进入文件。
    SQLite 仍是生命周期和节点运行事实源，档案在 Team 终态后从数据库重建。
    """

    def __init__(self) -> None:
        self._team_run_crud = AgentTeamRunCrud()
        self._conversation_run_crud = ConversationRunCrud()
        self._workspace_service = get_workspace_service()

    def write_terminal_archive(self, team_run_id: int) -> str | None:
        """为终态 TeamRun 写档案；文件写入失败时记录诊断并返回 None。

        档案属于 Agent 的辅助读取结果，写入失败不改变已经提交的 TeamRun 终态。调用方可用
        返回的 workspace 相对路径告知 Agent 如何查阅。
        """

        try:
            row = self._team_run_crud.get_by_id(team_run_id)
            if row.status not in _TERMINAL_STATUSES or row.started_at is None:
                return None
            workspace_root = Path(
                self._workspace_service.get_workspace(row.workspace_id).root_path
            ).resolve()
            target = workspace_agent_team_run_dir(workspace_root) / f"{row.id}.json"
            resolved, error = PathResolver(workspace_root).resolve_within_workspace(
                str(target), allow_reserved=True
            )
            if resolved is None:
                log.error(
                    "agent_team_run_archive_path_invalid",
                    extra={
                        "msg": "Agent Team 运行档案路径无法解析",
                        "data": {"team_run_id": row.id, "error": error},
                    },
                )
                return None

            document = self._build_document(row)
            content = json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ) + "\n"
            atomic_write_text(
                resolved,
                content,
                preserve_eol=False,
                containment_root=workspace_root,
            )
            relative_path = target.relative_to(workspace_root).as_posix()
            log.info(
                "agent_team_run_archive_written",
                extra={
                    "msg": "Agent Team 运行档案已写入 workspace",
                    "data": {
                        "team_run_id": row.id,
                        "workspace_id": row.workspace_id,
                        "path": relative_path,
                        "status": row.status,
                    },
                },
            )
            return relative_path
        except Exception:
            log.exception(
                "agent_team_run_archive_write_failed",
                extra={
                    "msg": "Agent Team 运行档案写入失败，不改变 TeamRun 终态",
                    "data": {"team_run_id": team_run_id},
                },
            )
            return None

    def existing_archive_path(self, team_run_id: int) -> str | None:
        """返回已生成档案的 workspace 相对路径；未启动或写入失败时返回 None。"""

        row = self._team_run_crud.get_by_id(team_run_id)
        if row.started_at is None or row.status not in _TERMINAL_STATUSES:
            return None
        workspace_root = Path(
            self._workspace_service.get_workspace(row.workspace_id).root_path
        ).resolve()
        target = workspace_agent_team_run_dir(workspace_root) / f"{row.id}.json"
        if not target.is_file():
            return None
        return target.relative_to(workspace_root).as_posix()

    def _build_document(self, row: AgentTeamRunModel) -> dict[str, Any]:
        """从 TeamRun、节点 Run 与冻结配置构造不含开发者标识的 JSON 投影。"""

        state = AgentTeamRunState.model_validate(row.state_json)
        configuration = row.configuration_snapshot_json
        node_names = {
            node["node_id"]: node["name"] for node in configuration["nodes"]
        }
        all_run_ids = [
            run_id
            for execution in state.node_executions.values()
            for run_id in execution.conversation_run_ids
        ]
        run_records = self._conversation_run_crud.get_many(
            list(dict.fromkeys(all_run_ids))
        )
        transitions_by_node: dict[str, list[dict[str, str | None]]] = defaultdict(list)
        for transition in state.transition_history:
            transitions_by_node[transition.from_node_id].append(
                {
                    "decision": transition.status,
                    "target_node_id": transition.target_node_id,
                }
            )

        visits: list[tuple[ConversationRunRecord, str, int]] = []
        for node_id, execution in state.node_executions.items():
            for visit_index, run_id in enumerate(execution.conversation_run_ids):
                visits.append((run_records[run_id], node_id, visit_index))
        visits.sort(key=lambda item: (item[0].created_at, item[0].id))

        history: list[dict[str, Any]] = []
        failure_node_id = (
            state.current_node_id
            if row.status == AgentTeamRunStatus.FAILED.value
            and row.end_reason in _NODE_FAILURE_REASONS
            else None
        )
        for run, node_id, visit_index in visits:
            node_status, output = self._parse_node_output(run.final_output)
            transitions = transitions_by_node[node_id]
            transition = transitions[visit_index] if visit_index < len(transitions) else None
            next_node_id = transition["target_node_id"] if transition is not None else None

            status = run.status
            if (
                row.status == AgentTeamRunStatus.CANCELLED.value
                and status == ConversationRunStatus.RUNNING.value
            ):
                status = ConversationRunStatus.CANCELLED.value
            elif (
                row.status == AgentTeamRunStatus.FAILED.value
                and node_id == failure_node_id
                and row.end_reason
                in {
                    AgentTeamRunEndReason.NODE_OUTPUT_INVALID.value,
                    AgentTeamRunEndReason.TRANSITION_NOT_FOUND.value,
                }
            ):
                status = ConversationRunStatus.FAILED.value

            history.append(
                {
                    "node": node_names[node_id],
                    "status": status,
                    "node_status": node_status,
                    "output": output,
                    "decision": transition["decision"] if transition is not None else None,
                    "next_node": (
                        node_names.get(next_node_id)
                        if next_node_id not in (None, "END")
                        else None
                    ),
                }
            )

        document: dict[str, Any] = {
            "team": configuration["name"],
            "goal": row.goal_input,
            "status": row.status,
            "history": history,
        }
        if row.status == AgentTeamRunStatus.FAILED.value:
            failure_node_id = failure_node_id or state.current_node_id
            document["failure"] = {
                "node": node_names.get(failure_node_id, "Team 启动阶段"),
                "reason": self._failure_reason(
                    row,
                    failure_node_id,
                    run_records,
                    state,
                ),
            }
        return document

    @staticmethod
    def _parse_node_output(final_output: str | None) -> tuple[str | None, str | None]:
        """从节点最终 JSON 中提取 Agent 声明的业务状态与面向下一节点的 output。"""

        if not final_output:
            return None, None
        try:
            value = json.loads(final_output)
        except json.JSONDecodeError:
            return None, None
        if not isinstance(value, dict):
            return None, None
        decision = value.get("status")
        output = value.get("output")
        return (
            decision if isinstance(decision, str) else None,
            output if isinstance(output, str) else None,
        )

    @staticmethod
    def _failure_reason(
        row: AgentTeamRunModel,
        failure_node_id: str | None,
        run_records: dict[int, ConversationRunRecord],
        state: AgentTeamRunState,
    ) -> str:
        """把持久化失败原因转为 Agent 可直接采取行动的中文说明。"""

        reason = row.end_reason
        if reason in {
            AgentTeamRunEndReason.NODE_RUN_FAILED.value,
            AgentTeamRunEndReason.NEXT_NODE_START_FAILED.value,
            AgentTeamRunEndReason.IMPLICIT_COMPLETION_MISSING.value,
        } and failure_node_id is not None:
            execution = state.node_executions.get(failure_node_id)
            if execution is not None and execution.conversation_run_ids:
                run = run_records[execution.conversation_run_ids[-1]]
                if run.status == ConversationRunStatus.FAILED.value:
                    return (run.error or {}).get("message") or run_failure_message(
                        run.end_reason
                    )
        messages = {
            AgentTeamRunEndReason.NODE_RUN_CANCELLED.value: "该节点的执行被取消，Team 因此未能完成。",
            AgentTeamRunEndReason.NODE_OUTPUT_INVALID.value: "该节点没有按要求返回有效的 decision 和 output。",
            AgentTeamRunEndReason.TRANSITION_NOT_FOUND.value: "该节点返回的 decision 没有对应的后续节点。",
            AgentTeamRunEndReason.IMPLICIT_COMPLETION_MISSING.value: "节点未明确结束 Team，也没有可继续的后续路径。",
            AgentTeamRunEndReason.MAX_RUNS_EXCEEDED.value: "已达到 Team 最大节点执行次数，目标尚未完成。",
            AgentTeamRunEndReason.RUNTIME_RESTARTED.value: "后端重启中断了 Team 执行。",
            AgentTeamRunEndReason.RUNTIME_UNAVAILABLE.value: "Team 执行环境不可用。",
            AgentTeamRunEndReason.TEAM_START_FAILED.value: "Team 启动阶段失败，尚未能正常执行节点。",
        }
        if reason == AgentTeamRunEndReason.NEXT_NODE_START_FAILED.value:
            if failure_node_id == row.configuration_snapshot_json["start_node_id"]:
                return "入口节点启动或调度失败，Team 未能开始正常执行。"
            return "后续节点启动或调度失败。"
        return messages.get(reason, "Team 执行失败，未能完成目标。")
