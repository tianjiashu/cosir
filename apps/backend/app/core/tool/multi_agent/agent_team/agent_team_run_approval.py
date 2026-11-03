"""Agent Team 审批门：TeamRun 在确认门槛上的持久化状态迁移。

``agent_team`` 工具是唯一调用方：首次调用创建 ``pending`` TeamRun，按用户决定执行时确认
（``pending → running``，写入最终执行计划）或驳回。这里只编排数据库迁移，不启动 Team——确认后由工具
在自己的线程里交给 Coordinator 阻塞运行；也不维护进程内待确认缓存。前端读模型归
``app.service.agent_team.agent_team_run_service``。
"""

from __future__ import annotations

from typing import Any

from app.config.logging.logger import log
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.tool.multi_agent.agent_team.agent_team_preparation_service import (
    AgentTeamPreparationResult,
    prepare_team_run,
)
from app.core.tool.multi_agent.args.node_goal import AgentTeamNodeGoal
from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.models.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.service.depends import get_workspace_service
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
        self,
        team_run_id: int,
        configuration_document: dict[str, Any],
        *,
        goal: str,
        node_goals: dict[str, AgentTeamNodeGoal],
        parent_agent: AgentRuntimeConfig,
    ) -> AgentTeamRunModel:
        """使用用户最终配置确认 TeamRun：``pending`` 原子迁移为 ``running`` 并写入最终执行计划。

        不启动 Coordinator：确认由 ``agent_team`` 工具在按用户决定执行时完成，随后由它阻塞运行
        Team。

        后端不信任预览展示数据，而是在确认边界按用户提交的最终配置重新校验并解析节点运行快照，
        与最终配置在同一事务里随 ``pending → running`` 原子迁移写入，所以 TeamRun 始终是唯一执行
        事实源。并发确认只有一个能迁移成功，落空的一方得到 ``ValueError``，不会重复启动。

        参数:
            parent_agent: 创建方案的主 Run 的 Agent 运行时配置，节点缺少独立模型配置时以它回落；
                必须是已物化模型设置的副本（注册表共享单例没有模型连接字段）。

        异常:
            ValueError: 配置无效、``team_id`` 与待确认 Team 不一致，或 TeamRun 已不在待确认态
                （已被确认、驳回或随主 Agent 取消）。
            TeamToolError: 最终配置的节点 Agent 或模型配置不可用。
            KeyError: TeamRun 不存在。
        """

        existing = self.get(team_run_id)
        configuration = AgentTeamConfiguration.model_validate(configuration_document)
        if existing.team_id != configuration.team_id:
            raise ValueError("确认配置的 team_id 与待确认 Team 不一致")
        final_goal = goal.strip()
        # 节点快照基于用户最终配置重新准备，初始预览产生的候选快照不作为确认依据。
        preparation = prepare_team_run(
            configuration,
            goal=final_goal,
            node_goals=node_goals,
            workspace_root=get_workspace_service().get_workspace(existing.workspace_id).root_path,
            parent_task_id=existing.parent_task_id,
            parent_agent=parent_agent,
        )

        with begin_immediate(self._session_factory) as session:
            updated = self._team_run_crud.update_status_if_in(
                existing.id,
                AgentTeamRunStatus.RUNNING.value,
                (AgentTeamRunStatus.PENDING.value,),
                state=AgentTeamRunState.initial(preparation.node_runtime_snapshots, configuration),
                started=True,
                configuration_snapshot_json=configuration.model_dump(mode="json"),
                goal_input=final_goal,
                session=session,
            )
        if updated is None:
            raise ValueError("该 Agent Team 方案已处理，不能再确认")
        return updated
