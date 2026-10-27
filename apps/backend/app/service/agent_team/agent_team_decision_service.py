"""Agent Team 审批决定的入队边界：同步校验用户决定，再以 HITL 决定消息交给 ``agent_team`` 工具。

审批门由后端强制：``agent_team`` 工具首次调用只创建 pending TeamRun 并返回等待占位，模型无法
自行把 TeamRun 推到运行。用户在卡片上按 ``teamRunId`` 提交决定，这里只做能同步回答用户的校验
——TeamRun 是否属于该 Task、是否仍待确认、批准载荷是否合法、同一方案是否已有未处理的决定——
通过后入队一条 ``DecisionBody``（按 TeamRun 记下的 ``tool_call_id`` 找回原调用）。批准与驳回的
副作用（确认并后台运行 Team / 落定驳回）都由工具在 ``prepare_input`` 绕过模型执行原调用时完成：
先确认消息再执行，进程中途退出也不会重复启动 Team；决定在 Run 被取消时留在外部队列，不会丢。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from app.config.logging.logger import log
from app.core.tool.schemas.tool_names import TOOL_AGENT_TEAM
from app.core.workflow.inbox.task_message import DecisionBody, TaskMessage
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.core.tool.handler.agent_team.args.agent_team_approve_input import AgentTeamApproveInput
from app.service.agent_team.agent_team_run_service import AgentTeamRunService
from app.service.depends import get_task_message_queue
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class AgentTeamDecisionService:
    def __init__(self) -> None:
        self._runs = AgentTeamRunService()
        self._queue = get_task_message_queue()

    async def submit(
        self,
        task_id: int,
        team_run_id: int,
        decision: Literal["approve", "reject"],
        data: dict[str, Any],
    ) -> None:
        """校验用户对一个待确认 TeamRun 的决定并入队；不等工具执行。

        批准载荷在这里按 ``AgentTeamApproveInput`` 校验形状，配置的领域校验仍在工具确认时做
        （``AgentTeamRunApproval.confirm``）：那一步要重新解析节点与工具，耗时且依赖运行期状态。
        重复提交的判定以外部队列为准；两次提交恰好并发时，后到的执行会发现 TeamRun 已不在待确认态，
        以失败结果收尾，不会重复启动。

        异常:
            KeyError: TeamRun 不存在。
            ValueError: TeamRun 不属于该 Task、已不在待确认态、已有未处理的决定，或批准载荷不合法
                （``ValidationError`` 是其子类）。
        """
        row = await asyncio.to_thread(self._runs.get, team_run_id)
        if row.parent_task_id != task_id:
            raise ValueError("TeamRun 不属于该对话")
        if row.status != AgentTeamRunStatus.PENDING.value:
            raise ValueError("该 Agent Team 方案已处理")
        pending = await asyncio.to_thread(self._queue.pending_nowait, task_id)
        if any(
            isinstance(message.body, DecisionBody) and message.body.tool_call_id == row.tool_call_id
            for message in pending
        ):
            raise ValueError("该 Agent Team 方案的决定已提交，正在处理")
        if decision == "approve":
            payload = AgentTeamApproveInput.model_validate(data).model_dump(mode="json")
            body = DecisionBody(
                tool_call_id=row.tool_call_id,
                tool_name=TOOL_AGENT_TEAM,
                decision="approve",
                data=payload,
            )
        else:
            feedback = data.get("feedback")
            body = DecisionBody(
                tool_call_id=row.tool_call_id,
                tool_name=TOOL_AGENT_TEAM,
                decision="reject",
                feedback=feedback if isinstance(feedback, str) and feedback.strip() else None,
            )
        await asyncio.to_thread(task_runtime_spaces.enqueue, task_id, TaskMessage.of(body))
        log.info(
            "agent_team_decision_enqueued",
            extra={
                "msg": "Agent Team 审批决定已入队，等待工具执行",
                "data": {"task_id": task_id, "team_run_id": team_run_id, "decision": decision},
            },
        )
