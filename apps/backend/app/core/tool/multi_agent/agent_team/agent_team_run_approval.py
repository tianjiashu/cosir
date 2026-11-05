"""Agent Team 审批门：TeamRun 在确认门槛上的持久化状态迁移。

``agent_team`` 首次调用创建待批 TeamRun。批准时重新解析配置，成功后交给 Coordinator，
配置失效则结束为 failed。前端读模型归 ``app.service.agent_team.agent_team_run_service``。
"""

from __future__ import annotations

from app.config.logging.logger import log
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.tool.multi_agent.agent_team.agent_team_preparation_service import (
    AgentTeamPreparationResult,
    prepare_team_run,
)
from app.core.tool.multi_agent.agent_team.team_tool_error import TeamToolError
from app.core.tool.multi_agent.args.agent_team_approve_input import AgentTeamApproveInput
from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.models.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.service.depends import get_conversation_run_ledger, get_workspace_service
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate


class AgentTeamRunApproval:
    """管理 TeamRun 从待确认到运行（或驳回）的确认门槛迁移。"""

    def __init__(self) -> None:
        self._team_run_crud = AgentTeamRunCrud()
        self._session_factory = main_session_factory()

    def create_pending_confirmation(
        self,
        *,
        configuration: AgentTeamConfiguration,
        preparation: AgentTeamPreparationResult,
        workspace_id: int,
        parent_task_id: int,
        parent_run_id: int,
        tool_call_id: str,
        goal: str,
    ) -> AgentTeamRunModel:
        """创建并持久化一条处于确认门槛的 ``pending`` TeamRun。

        初始配置快照和节点运行快照作为待确认候选保存；用户确认时会使用请求体中的最终
        配置重新准备并覆盖这些执行快照。预览文档只返回给工具层，不写入 TeamRun。创建
        后的 ``pending`` 记录不会进入 Coordinator 的活动运行集合。

        参数:
            configuration: 已通过领域校验的 Team 配置。
            preparation: ``prepare`` 生成的不可变执行计划。
            workspace_id: TeamRun 所属 workspace 主键。
            parent_task_id: 发起 TeamRun 的主 Agent Task 主键。
            parent_run_id: 发起 TeamRun 的主 ConversationRun 主键。
            tool_call_id: 创建本方案的工具调用身份，用户决定据此找回原调用。
            goal: 本次 Team 总目标（会被 ``strip``）。

        返回:
            持久化后的 ``pending`` TeamRun 模型。

        异常:
            数据库异常原样上浮，由工具执行层统一记录并转成失败观察——它不是模型改参数或用户
            改配置能修好的错误，不包装成 ``TeamToolError``。
        """

        with begin_immediate(self._session_factory) as session:
            # 先收敛同一主 Run 遗留的 pending：主 Run 内同时只允许一条待确认方案（部分唯一
            # 索引约束），用户驳回后重新提案若不先收敛旧行就会撞唯一索引，使驳回变成死路。
            superseded = self._team_run_crud.supersede_pending_for_parent(
                parent_task_id=parent_task_id,
                parent_run_id=parent_run_id,
                session=session,
            )
            created = self._team_run_crud.create(
                configuration=configuration,
                workspace_id=workspace_id,
                parent_task_id=parent_task_id,
                parent_run_id=parent_run_id,
                tool_call_id=tool_call_id,
                goal=goal.strip(),
                node_runtime_snapshots=preparation.node_runtime_snapshots,
                session=session,
            )
        if superseded:
            log.info(
                "agent_team_pending_superseded",
                extra={
                    "msg": "新方案替代了旧的待确认 TeamRun",
                    "data": {
                        "parent_run_id": parent_run_id,
                        "superseded_count": superseded,
                        "new_team_run_id": created.id,
                    },
                },
            )
        return created

    def get_for_tool_call(self, parent_task_id: int, tool_call_id: str) -> AgentTeamRunModel:
        """读取某次 ``agent_team`` 调用创建的 TeamRun；不存在抛 ``KeyError``。"""

        return self._team_run_crud.get_by_tool_call(parent_task_id, tool_call_id)

    def get(self, run_id: int) -> AgentTeamRunModel:
        """按主键读取 TeamRun；不存在时 CRUD 抛 ``KeyError``。"""

        return self._team_run_crud.get_by_id(run_id)

    def reject_pending_confirmation(self, team_run_id: int) -> AgentTeamRunModel:
        """按用户驳回决定结束待确认 TeamRun，并使重复决定保持幂等。"""

        with begin_immediate(self._session_factory) as session:
            rejected = self._team_run_crud.update_status_if_in(
                team_run_id,
                AgentTeamRunStatus.CANCELLED.value,
                (AgentTeamRunStatus.PENDING.value,),
                end_reason=AgentTeamRunEndReason.USER_REJECTED.value,
                ended=True,
                session=session,
            )
            if rejected is not None:
                return rejected
            current = self._team_run_crud.get_by_id(team_run_id, session=session)
        # 重复的驳回落空时当前行已是「用户驳回」，照样返回；只有被其他路径处理才值得记一笔。
        if current.end_reason != AgentTeamRunEndReason.USER_REJECTED.value:
            log.info(
                "agent_team_rejection_already_resolved",
                extra={
                    "msg": "驳回到达时 TeamRun 已由其他路径处理",
                    "data": {"team_run_id": team_run_id, "status": current.status},
                },
            )
        return current

    def confirm(
        self, row: AgentTeamRunModel, inputs: AgentTeamApproveInput,
    ) -> AgentTeamRunModel | None:
        """按已认领的批准决定重新解析最终配置，从提出方案的 Run 取节点回落配置。

        确认成功时随 pending → running 写入执行计划。配置失效则落定 failed，抛 TeamToolError
        将原因交给原工具调用。两种迁移都只接受 pending；已由其他路径处理时返回 None。
        """
        final_goal = inputs.goal.strip()
        try:
            configuration = AgentTeamConfiguration.model_validate(inputs.configuration)
            if row.team_id != configuration.team_id:
                raise ValueError("确认配置的 team_id 与待确认 Team 不一致")
            preparation = prepare_team_run(
                configuration,
                goal=final_goal,
                node_goals=inputs.node_goals,
                workspace_root=get_workspace_service().get_workspace(row.workspace_id).root_path,
                parent_task_id=row.parent_task_id,
                parent_agent=AgentRuntimeConfig.for_run(get_conversation_run_ledger().get_run(row.parent_run_id)),
            )
            state = AgentTeamRunState.initial(preparation.node_runtime_snapshots, configuration)
        except (TeamToolError, ValueError, KeyError, RuntimeError) as exc:
            failed = self._team_run_crud.update_status_if_in(
                row.id, AgentTeamRunStatus.FAILED.value, (AgentTeamRunStatus.PENDING.value,),
                end_reason=AgentTeamRunEndReason.TEAM_START_FAILED.value, ended=True,
            )
            if failed is None:
                return None
            log.warning("agent_team_confirm_rejected", extra={
                "msg": "已批准的 Team 配置无法启动，方案已结束",
                "data": {"team_run_id": row.id, "reason": str(exc)},
            })
            raise TeamToolError(str(exc)) from exc

        return self._team_run_crud.update_status_if_in(
            row.id, AgentTeamRunStatus.RUNNING.value, (AgentTeamRunStatus.PENDING.value,),
            state=state, started=True,
            configuration_snapshot_json=configuration.model_dump(mode="json"),
            goal_input=final_goal,
        )
