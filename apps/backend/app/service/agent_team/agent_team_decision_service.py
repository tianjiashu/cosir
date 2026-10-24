"""Agent Team 审批决定：确定性执行用户决定，批准后在后台运行 Team，并以消息唤醒主 Agent。

审批门由后端强制：``agent_team`` 工具只创建 pending TeamRun 并挂起主 Agent，模型无法自行把
TeamRun 推到运行。用户在卡片上提交决定后：

- 批准：在确认边界重新校验配置并原子迁移 ``pending → running``，随后把 Coordinator 交给后台任务
  执行；Team 结束时投递一条后台结果消息。
- 驳回：TeamRun 落定 cancelled（``user_rejected``）。

两者都投递一条决定消息唤醒主 Agent。决定校验只在这里做一次：TeamRun 不属于该 Task 或已不在待确认
态时直接拒绝（接入层映射 409），不经工作流转手。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from app.config.logging.logger import log
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.task_message import BackgroundResultBody, DecisionBody
from app.service.agent_team.agent_team_approve_input import AgentTeamApproveInput
from app.service.agent_team.agent_team_run_archive_service import AgentTeamRunArchiveService
from app.service.agent_team.agent_team_run_service import AgentTeamRunService
from app.service.conversation_run.workflow_session import SessionReceipt
from app.service.depends import get_conversation_run_service, get_workflow_session
from app.storage.model.agent_team_run_model import AgentTeamRunModel

# 后台执行的 Team：持有引用，避免事件循环只保留弱引用时任务被回收。
_BACKGROUND_TEAMS: set[asyncio.Task[None]] = set()


class AgentTeamDecisionService:
    def __init__(self) -> None:
        self._runs = AgentTeamRunService()
        self._archives = AgentTeamRunArchiveService()

    async def submit(
        self,
        task_id: int,
        team_run_id: int,
        decision: Literal["approve", "reject"],
        data: dict[str, Any],
    ) -> SessionReceipt:
        """执行用户对一个待确认 TeamRun 的决定，再投递决定消息唤醒主 Agent。

        异常:
            KeyError: TeamRun 或主 Agent Run 不存在。
            ValueError: TeamRun 不属于该 Task、已不在待确认态，或批准输入不合法。
        """
        row = await asyncio.to_thread(self._runs.get, team_run_id)
        if row.parent_task_id != task_id:
            raise ValueError("TeamRun 不属于该对话")
        if decision == "approve":
            confirmed = await asyncio.to_thread(self._confirm, row, data)
            self._start_in_background(confirmed)
            body = DecisionBody(team_run_id=team_run_id, decision="approve")
        else:
            await asyncio.to_thread(self._reject, team_run_id)
            feedback = data.get("feedback")
            body = DecisionBody(
                team_run_id=team_run_id,
                decision="reject",
                feedback=feedback if isinstance(feedback, str) and feedback.strip() else None,
            )
        log.info(
            "agent_team_decision_applied",
            extra={
                "msg": "Agent Team 审批决定已执行",
                "data": {"task_id": task_id, "team_run_id": team_run_id, "decision": decision},
            },
        )
        return await get_workflow_session().post_async(task_id, body)

    def _confirm(self, row: AgentTeamRunModel, data: dict[str, Any]) -> AgentTeamRunModel:
        from app.service.conversation_run.run_driver import resolve_agent_config

        inputs = AgentTeamApproveInput.model_validate(data)
        # 节点缺少独立模型连接配置时回落到主 Agent 的运行时配置：只能由主 Run 的持久化事实合成，
        # 不能从注册表单例取（内置 Agent 没有模型连接字段）。
        parent_agent = resolve_agent_config(get_conversation_run_service().get_run(row.parent_run_id))
        return self._runs.confirm(
            row.id,
            inputs.configuration,
            goal=inputs.goal,
            node_goals=inputs.node_goals,
            parent_agent=parent_agent,
        )

    def _reject(self, team_run_id: int) -> None:
        current = self._runs.reject_pending_confirmation(team_run_id)
        if (
            current.status != AgentTeamRunStatus.CANCELLED.value
            or current.end_reason != AgentTeamRunEndReason.USER_REJECTED.value
        ):
            raise ValueError("该 Agent Team 方案已处理，不能再驳回")

    def _start_in_background(self, row: AgentTeamRunModel) -> None:
        """在事件循环上登记 Team 的后台执行；Coordinator 是阻塞循环，放进工作线程。"""
        task = asyncio.create_task(self._run_team(row))
        _BACKGROUND_TEAMS.add(task)
        task.add_done_callback(_BACKGROUND_TEAMS.discard)

    async def _run_team(self, row: AgentTeamRunModel) -> None:
        """跑完 Team 并投递结果；后台任务无人 await，异常在这里记录。

        结果总是投递：主 Agent 被用户取消时由 ``WorkflowSession.post_async`` 丢弃；用户在卡片上
        取消 Team 时主 Agent 仍在等，需要知道 Team 已停止。
        """
        from app.agent_team.coordinator import get_agent_team_coordinator

        loop = asyncio.get_running_loop()
        try:
            final = await asyncio.to_thread(get_agent_team_coordinator().start, row, loop)
        except Exception:
            # Coordinator 已把调度异常收敛到 TeamRun（failed）并记录堆栈，这里读取落定事实。
            log.exception(
                "agent_team_background_run_failed",
                extra={"msg": "Agent Team 后台执行异常结束", "data": {"team_run_id": row.id}},
            )
            final = await asyncio.to_thread(self._runs.get, row.id)
        try:
            body = await asyncio.to_thread(self._result_body, final)
            await get_workflow_session().post_async(row.parent_task_id, body)
        except Exception:
            log.exception(
                "agent_team_result_post_failed",
                extra={"msg": "Agent Team 结果投递失败", "data": {"team_run_id": row.id}},
            )

    def _result_body(self, row: AgentTeamRunModel) -> BackgroundResultBody:
        archive_path = self._archives.existing_archive_path(row.id)
        payload: dict[str, Any] = {
            "status": row.status,
            "end_reason": row.end_reason,
            "trace_file": archive_path,
            "read_instructions": _archive_read_instructions(archive_path),
        }
        if row.status == AgentTeamRunStatus.COMPLETED.value:
            payload["hint"] = "Review the node outputs in the saved run history against the goal."
        return BackgroundResultBody(
            job="agent_team",
            job_id=row.id,
            status=row.status,
            content=json.dumps(payload, ensure_ascii=False),
        )


def _archive_read_instructions(archive_path: str | None) -> str:
    """给 Agent 返回现有 read_file 的分段读取方法，不要求整份档案一次装入上下文。"""

    if archive_path is None:
        return "The run history file is not available."
    return (
        f'Read with read_file(path="{archive_path}", offset=1, limit=200). '
        "Inspect the top-level status and failure first, then read history in pages; "
        "when the result says to continue with a later offset, use that offset rather "
        "than loading the whole file. Each history entry contains one node's status, "
        "output, decision, and next node. If the file exceeds read_file's size limit or "
        "an output line is truncated, use execute_terminal with Python's json module to "
        "load the document and print only the needed history entry or a small output slice."
    )
