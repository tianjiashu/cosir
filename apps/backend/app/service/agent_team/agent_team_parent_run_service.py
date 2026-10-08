"""Agent Team 对主 ConversationRun 的等待、消息交接与续跑编排。"""

from __future__ import annotations

import asyncio
import json

from langchain_core.messages import SystemMessage

from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.config.logging.logger import log
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.agent_team.agent_team_run_service import AgentTeamRunService
from app.service.conversation_run.conversation_run_service import ConversationRunService
from app.service.depends import get_conversation_run_service
from app.storage.model.agent_team_run_model import AgentTeamRunModel

_PARENT_WAIT_ATTEMPTS = 600
_PARENT_POLL_INTERVAL_SECONDS = 0.05


class AgentTeamParentRunService:
    """协调 TeamRun 与发起它的主 ConversationRun 之间的交接。

    Team 节点调度仍由 ``AgentTeamCoordinator`` 负责。本服务只处理主 Run 的 interrupt
    收尾、延迟系统消息注入与 checkpoint 恢复，避免 Team 图协调器直接编排对话 Run 生命周期。
    """

    def __init__(self) -> None:
        """创建主 Run 交接服务。"""

        self._team_run_service = AgentTeamRunService()
        self._run_service = get_conversation_run_service()

    async def wait_for_parent_input(self, team_run_id: int) -> None:
        """等待预览所属主 Run 完成 interrupt 和旧执行器收尾。

        工具结果可能先于 LangGraph interrupt 和执行器收尾抵达前端，因此确认或驳回请求
        必须等到主 Run 进入 ``waiting_for_input`` 且旧执行器停止后再处理。

        参数:
            team_run_id: 预览对应的 TeamRun 标识。

        异常:
            KeyError: TeamRun 或其主 Run 不存在。
            ValueError: 主 Run 已结束、未及时进入等待状态，或旧执行器未及时结束。
        """

        team = await asyncio.to_thread(self._team_run_service.get, team_run_id)
        for _ in range(_PARENT_WAIT_ATTEMPTS):
            parent_run = await asyncio.to_thread(
                self._run_service.get_run,
                team.parent_run_id,
            )
            if parent_run.status == ConversationRunStatus.WAITING_FOR_INPUT.value:
                await self._wait_for_executor(team.parent_run_id)
                return
            if parent_run.status in _TERMINAL_PARENT_STATUSES:
                raise ValueError("主 Agent Run 已结束，不能处理 Team 预览")
            await asyncio.sleep(_PARENT_POLL_INTERVAL_SECONDS)
        raise ValueError("主 Agent Run 尚未进入等待输入状态，请稍后重试")

    async def resume_parent_after_rejection(
        self,
        team: AgentTeamRunModel,
        goal: str,
        node_goals: dict[str, str],
        feedback: str,
    ) -> None:
        """把审阅后的运行输入和驳回意见交给主 Agent，恢复其等待中的 checkpoint。

        参数:
            team: 仍处于 ``pending`` 的 TeamRun；新预览创建后由创建流程将其标记为已替代。
            goal: 用户在预览中最终审阅的总目标。
            node_goals: 用户在预览中最终审阅的完整节点子目标映射。
            feedback: 必填的用户驳回意见。

        异常:
            KeyError: TeamRun 或其主 Run 不存在。
            ValueError: TeamRun 不处于待确认状态，或主 Run 已不处于等待用户输入状态。
            RuntimeError: 执行器无法启动恢复后的 Run。

        副作用:
            更新主 Run 状态并以 resume 模式启动既有 checkpoint。
        """

        if team.status != AgentTeamRunStatus.PENDING.value:
            raise ValueError("Agent Team 已确认或已处理")
        state = AgentTeamRunState.model_validate(team.state_json)
        if set(node_goals) != set(state.node_runtime):
            raise ValueError("节点子目标必须完整覆盖当前 Team 的所有节点")
        reviewed_input = json.dumps(
            {
                "team_id": team.team_id,
                "goal": goal,
                "node_goals": node_goals,
                "feedback": feedback,
            },
            ensure_ascii=False,
            indent=2,
        )
        content = (
            "The user rejected the pending Agent Team preview and provided required feedback. "
            "This is a request to revise and resubmit the proposal, not approval to execute it.\n\n"
            "Call the `agent_team` tool with the same team_id and the reviewed goal and complete "
            "node_goals mapping below to create a new preview. Use the rejection feedback to "
            "make the requested refinements, and do not revert user-edited values to the original "
            "tool-call values. Wait for user confirmation before execution.\n\n"
            "User-reviewed inputs (JSON):\n"
            f"{reviewed_input}"
        )
        try:
            await self._resume_waiting_parent(team, content)
        except Exception:
            log.exception(
                "agent_team_rejection_parent_resume_failed",
                extra={
                    "msg": "Agent Team 驳回后恢复主 Agent Run 失败",
                    "data": {
                        "team_run_id": team.id,
                        "parent_task_id": team.parent_task_id,
                        "parent_run_id": team.parent_run_id,
                    },
                },
            )
            raise

    async def resume_parent_after_team(
        self,
        team: AgentTeamRunModel,
        result_message: str,
    ) -> bool:
        """等待主 Run 到达 interrupt 边界后交付 Team 结果并恢复 checkpoint。

        参数:
            team: 已进入终态的 TeamRun。
            result_message: 序列化后的 Team 结果消息。

        返回:
            主 Run 成功恢复时为 ``True``；主 Run 已结束或等待超时则为 ``False``。

        异常:
            KeyError: TeamRun 或其主 Run 不存在。
            ValueError: 主 Run 状态不满足恢复契约。
            RuntimeError: 执行器无法启动恢复后的 Run。
        """

        for _ in range(_PARENT_WAIT_ATTEMPTS):
            parent_run = await asyncio.to_thread(
                self._run_service.get_run,
                team.parent_run_id,
            )
            if parent_run.status == ConversationRunStatus.WAITING_FOR_INPUT.value:
                await self._resume_waiting_parent(team, result_message)
                return True
            if parent_run.status in _TERMINAL_PARENT_STATUSES:
                break
            await asyncio.sleep(_PARENT_POLL_INTERVAL_SECONDS)

        parent_run = await asyncio.to_thread(
            self._run_service.get_run,
            team.parent_run_id,
        )
        log.warning(
            "agent_team_parent_run_not_waiting",
            extra={
                "msg": "Agent Team 已结束，但主 Agent Run 不在等待输入状态",
                "data": {
                    "team_run_id": team.id,
                    "parent_run_id": team.parent_run_id,
                    "parent_status": parent_run.status,
                },
            },
        )
        return False

    async def _resume_waiting_parent(
        self, team: AgentTeamRunModel, content: str
    ) -> None:
        """为等待中的主 Run 排入消息、原子恢复状态并启动现有 checkpoint。"""

        parent_run = await asyncio.to_thread(
            self._run_service.get_run,
            team.parent_run_id,
        )
        if parent_run.status != ConversationRunStatus.WAITING_FOR_INPUT.value:
            raise ValueError("主 Agent Run 当前没有等待用户输入")

        await self._wait_for_executor(team.parent_run_id)

        from app.service.depends import (
            get_conversation_run_command_service,
            get_conversation_run_executor,
        )
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        resumed = await asyncio.to_thread(
            get_conversation_run_command_service().resume_run,
            team.parent_task_id,
            team.parent_run_id,
            expected_status=ConversationRunStatus.WAITING_FOR_INPUT,
        )
        task_runtime_spaces.get_or_create(team.parent_task_id).defer_system_message(
            SystemMessage(
                content=content,
                additional_kwargs={"run_id": team.parent_run_id},
            )
        )
        await get_conversation_run_executor().start(
            resumed.run.id,
            start_mode=resumed.execution_mode,
        )

    @staticmethod
    async def _wait_for_executor(run_id: int) -> None:
        """等待该 Run 的旧执行器结束，避免与恢复后的执行器重叠。"""

        from app.service.depends import get_conversation_run_executor

        await get_conversation_run_executor().wait_until_stopped(run_id)


_TERMINAL_PARENT_STATUSES = frozenset(
    {
        ConversationRunStatus.COMPLETED.value,
        ConversationRunStatus.FAILED.value,
        ConversationRunStatus.CANCELLED.value,
    }
)
