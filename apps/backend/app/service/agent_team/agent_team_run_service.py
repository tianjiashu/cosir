"""Agent Team 执行意图与运行生命周期应用服务。

本模块把待确认和运行中的 Team 统一视为同一个持久化 TeamRun，只编排数据库状态迁移和
Coordinator 启动，不负责节点调度细节，也不维护进程内待确认缓存。
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.agent_team.team_tool_error import TeamToolError
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.model_settings import ModelSettings
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.service.agent_team.agent_team_preparation_service import (
    AgentTeamPreparationResult,
    AgentTeamPreparationService,
)
from app.service.conversation_run.conversation_run_service import ConversationRunService
from app.service.depends import (
    get_conversation_run_service,
    get_model_config_service,
    get_workspace_service,
)
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate

# 主 Run 的终态集合：确认边界只排除它们，不再要求主 Run 停在等待态（见 ``_validate_parent_run``）。
_TERMINAL_PARENT_STATUSES = frozenset({"completed", "failed", "cancelled"})


class AgentTeamRunService:
    """管理 TeamRun 从待确认到运行的持久化生命周期。"""

    def __init__(self) -> None:
        """创建无进程状态的 TeamRun 生命周期服务。

        参数:
            team_run_crud: 可选的 TeamRun CRUD，供测试或特殊装配注入。
            run_service: 可选的主 Agent Run 读取服务。
        """

        self._team_run_crud = AgentTeamRunCrud()
        self._run_service = get_conversation_run_service()
        self._session_factory = main_session_factory()

    def create_pending_confirmation(
        self,
        *,
        configuration: AgentTeamConfiguration,
        preparation: AgentTeamPreparationResult,
        workspace_id: int,
        parent_task_id: int,
        parent_run_id: int,
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
            goal: 本次 Team 总目标（会被 ``strip``）。

        返回:
            持久化后的 ``pending`` TeamRun 模型。

        异常:
            TeamToolError: 数据库写入失败（不可重试）时抛出。
        """

        goal_input = goal.strip()
        try:
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
                    goal=goal_input,
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
        except SQLAlchemyError as exc:
            raise TeamToolError(
                f"Agent Team 待确认记录持久化失败: {exc}",
                retryable=False,
            ) from exc

    def get(self, run_id: int) -> AgentTeamRunModel:
        """读取 TeamRun。

        未找到时由 CRUD 直接抛出 ``KeyError``，交由 API 层统一映射为 404；本方法不吞咽
        异常、不返回 ``None``，避免「未找到」契约在链路中转。
        """

        with self._session_factory() as session:
            return self._team_run_crud.get_by_id(run_id, session=session)

    def confirm_and_start(
        self,
        team_run_id: int,
        configuration_document: dict[str, Any],
        *,
        goal: str,
        node_goals: dict[str, str],
        parent_agent_profile: AgentProfile,
    ) -> AgentTeamRunModel:
        """使用用户最终配置确认 TeamRun，并在提交后交给 Coordinator 启动。

        前端提交工具预览关联的 TeamRun ID 和用户最终配置。后端不信任预览展示数据或
        前端指纹，而是在确认边界重新校验配置、解析节点运行快照并计算指纹，再将最终
        执行计划写入 TeamRun。确认事务会把 TeamRun 从 ``pending`` 原子迁移为 ``running``，
        并同时写入最终配置和运行快照；事务提交后才启动 Coordinator，因此数据库中的
        TeamRun 始终是唯一执行事实源。两个并发确认请求中只有一个能够成功迁移状态。

        幂等：条件迁移未命中（``rowcount == 0``）时不再抛错，而是返回该行的当前事实。
        human-in-the-loop 的批准由图重放投递，进程在「已启动」与「写 checkpoint」之间终止会
        重复确认；此时必须如实返回「已在运行 / 已结束」，既不重复启动 Coordinator，也不让
        调用方误判为失败。

        参数:
            parent_agent_profile: 本次确认所属主 Run 的 per-run Agent profile（由运行时注入）。
                节点缺少独立模型连接配置时以它回落，因此必须由调用方传入已物化模型设置的副本；
                本层不再自行解析注册表（共享单例没有模型连接字段，会让准备阶段抛 ``KeyError``）。

        异常:
            ValueError: TeamRun 不存在、Team 标识或配置无效、主 Run 不可恢复，或同一 pending
                行被两个并发确认同时命中（真正的竞争，调用方应重试）。
            KeyError: 关联的主 Agent Run 不存在。
        """

        existing = self.get(team_run_id)
        self._validate_parent_run(existing.parent_task_id, existing.parent_run_id)
        final_goal = goal.strip()
        configuration:AgentTeamConfiguration = AgentTeamConfiguration.model_validate(configuration_document)
        final_node_goals = {
            key: value.strip()
            for key, value in node_goals.items()
            if isinstance(key, str) and isinstance(value, str) and value.strip()
        }
        agent_team_preparation:AgentTeamPreparationResult = self._prepare_final_plan(
            existing,
            configuration,
            goal=final_goal,
            node_goals=final_node_goals,
            parent_agent_profile=parent_agent_profile,
        )

        with begin_immediate(self._session_factory) as session:
            if existing.team_id != configuration.team_id:
                raise ValueError("确认配置的 team_id 与待确认 Team 不一致")
            state = AgentTeamRunState.initial(agent_team_preparation.node_runtime_snapshots,configuration)
            updated = self._team_run_crud.update_status_if_in(
                existing.id,
                AgentTeamRunStatus.RUNNING.value,
                (AgentTeamRunStatus.PENDING.value,),
                state=state,
                started=True,
                configuration_snapshot_json=configuration.model_dump(mode="json"),
                goal_input=final_goal,
                session=session,
            )
            if updated is None:
                latest = self._team_run_crud.get_by_id(existing.id, session=session)
                if latest.status == AgentTeamRunStatus.PENDING.value:
                    # 事务内读到的状态仍是 pending 却迁移失败，只可能是并发确认刚刚提交；
                    # 交给调用方重试，不静默返回一个未启动的运行。
                    raise ValueError("Agent Team 确认竞争失败，请稍后重试")
                log.info(
                    "agent_team_confirmation_replayed",
                    extra={
                        "msg": "TeamRun 已被确认过，按当前事实返回而不重复启动",
                        "data": {"team_run_id": existing.id, "status": latest.status},
                    },
                )
                return latest

        from app.agent_team.coordinator import get_agent_team_coordinator

        return get_agent_team_coordinator().start(
            updated,
        )

    def _prepare_final_plan(
        self,
        row: AgentTeamRunModel,
        configuration: AgentTeamConfiguration,
        *,
        goal: str,
        node_goals: dict[str, str],
        parent_agent_profile: AgentProfile,
    ) -> AgentTeamPreparationResult:
        """基于确认时的最终配置重新生成执行快照。

        该方法只读取 workspace 与运行时注册表（节点 Profile 与工具），不写数据库。返回结果由
        确认事务统一写入 TeamRun；初始工具预览产生的候选快照不作为确认依据。

        参数:
            parent_agent_profile: 与 :meth:`confirm_and_start` 同一份 per-run profile，供节点
                在缺少独立模型连接配置时回落。
        """

        workspace_root = get_workspace_service().get_workspace(row.workspace_id).root_path
        return AgentTeamPreparationService().prepare(
            configuration,
            goal=goal,
            node_goals=node_goals,
            workspace_root=workspace_root,
            parent_task_id=row.parent_task_id,
            parent_run_id=row.parent_run_id,
            workspace_id=row.workspace_id,
            parent_agent_profile=parent_agent_profile,
        )

    def _validate_parent_run(self, parent_task_id: int, parent_run_id: int) -> None:
        """校验 TeamRun 关联的主 Agent Run 仍属于该 Task 且可继续。

        只校验归属与「未终态」：user-input 决定由 graph 重放投递，批准发生在主 Run 已由
        续跑入口迁移为 ``running`` 之后（``resume_waiting_run``），因此这里**不能**再要求
        ``waiting_for_input``——那会让每一次真实批准都失败（旧 REST 流程的前置
        ``wait_for_parent_input`` 已随专用端点删除）。
        """

        parent_run = self._run_service.get_run(parent_run_id)
        if parent_run.task_id != parent_task_id:
            raise ValueError("TeamRun 不属于指定主 Agent Task")
        if parent_run.status in _TERMINAL_PARENT_STATUSES:
            raise ValueError("主 Agent Run 已结束，不能确认 TeamRun")
