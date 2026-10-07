"""Agent Team 执行意图与运行生命周期应用服务。

本模块把待确认和运行中的 Team 统一视为同一个持久化 TeamRun，只编排数据库状态迁移和
Coordinator 启动，不负责节点调度细节，也不维护进程内待确认缓存。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.config.configuration import get_agent_registry
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.model_settings import ModelSettings
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.service.agent_team.agent_team_preparation_service import (
    AgentTeamPreparationResult,
    AgentTeamPreparationService,
)
from app.service.depends import get_model_config_service, get_workspace_service
from app.service.conversation_run.conversation_run_service import ConversationRunService
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate


class AgentTeamRunService:
    """管理 TeamRun 从待确认到运行的持久化生命周期。"""

    def __init__(
        self,
        *,
        team_run_crud: AgentTeamRunCrud | None = None,
        run_service: ConversationRunService | None = None,
    ) -> None:
        """创建无进程状态的 TeamRun 生命周期服务。

        参数:
            team_run_crud: 可选的 TeamRun CRUD，供测试或特殊装配注入。
            run_service: 可选的主 Agent Run 读取服务。
        """

        self._team_run_crud = team_run_crud or AgentTeamRunCrud()
        self._run_service = run_service or ConversationRunService()
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
        instructions: dict[str, str],
    ) -> AgentTeamRunModel:
        """创建并持久化一条处于确认门槛的 ``pending`` TeamRun。

        初始配置快照和节点运行快照作为待确认候选保存；用户确认时会使用请求体中的最终
        配置重新准备并覆盖这些执行快照。预览文档只返回给工具层，不写入 TeamRun。创建
        后的 ``pending`` 记录不会进入 Coordinator 的活动运行集合。
        相同主 Task/Run 的旧待确认 TeamRun 会在同一事务中标记为 ``cancelled``，避免一个
        主 Run 同时存在多个确认入口。

        异常:
            ValueError: 输入或准备结果无效时向上抛出。
            sqlalchemy.exc.SQLAlchemyError: 主库写入失败时向上抛出。
        """

        instructions = {
            key: value.strip() for key, value in instructions.items() if value.strip()
        }
        with begin_immediate(self._session_factory) as session:
            self._team_run_crud.cancel_pending_confirmation_for_parent(
                parent_task_id,
                parent_run_id,
                session=session,
            )
            row = self._team_run_crud.create(
                configuration=configuration,
                workspace_id=workspace_id,
                parent_task_id=parent_task_id,
                parent_run_id=parent_run_id,
                preview_fingerprint=preparation.preview_fingerprint,
                goal=goal.strip(),
                instructions=instructions,
                node_runtime_snapshots=preparation.node_runtime_snapshots,
                session=session,
            )
            return row

    def get(self, run_id: int) -> AgentTeamRunModel | None:
        """读取 TeamRun。"""

        with self._session_factory() as session:
            try:
                return self._team_run_crud.get_by_id(run_id, session=session)
            except KeyError:
                return None

    def reject_pending(self, team_run_id: int) -> AgentTeamRunModel:
        """原子驳回待确认 TeamRun。

        参数:
            team_run_id: 前端预览卡关联的 TeamRun 标识。

        返回:
            已迁移为 ``cancelled`` 且原因是用户驳回的 TeamRun 记录。

        异常:
            KeyError: TeamRun 不存在。
            ValueError: TeamRun 已被处理，或其主 Run 当前不在等待用户输入状态。
            sqlalchemy.exc.SQLAlchemyError: 条件状态更新失败。

        副作用:
            以条件更新结束 TeamRun；主 Agent 反馈注入和 checkpoint 恢复由 Coordinator 执行。
        """

        existing = self.get(team_run_id)
        if existing is None:
            raise KeyError(team_run_id)
        if existing.status != AgentTeamRunStatus.PENDING.value:
            raise ValueError("Agent Team 已确认或已处理")
        self._validate_parent_run(existing.parent_task_id, existing.parent_run_id)
        updated = self._team_run_crud.update_status_if_in(
            existing.id,
            AgentTeamRunStatus.CANCELLED.value,
            (AgentTeamRunStatus.PENDING.value,),
            end_reason=AgentTeamRunEndReason.REJECTED_BY_USER.value,
            ended=True,
        )
        if updated is None:
            raise ValueError("Agent Team 已确认或已处理")
        return updated

    def confirm_and_start(
        self,
        team_run_id: int,
        configuration_document: dict[str, Any],
        *,
        goal: str | None = None,
        instructions: dict[str, str] | None = None,
        runtime_loop: asyncio.AbstractEventLoop,
    ) -> AgentTeamRunModel:
        """使用用户最终配置确认 TeamRun，并在提交后交给 Coordinator 启动。

        前端提交工具预览关联的 TeamRun ID 和用户最终配置。后端不信任预览展示数据或
        前端指纹，而是在确认边界重新校验配置、解析节点运行快照并计算指纹，再将最终
        执行计划写入 TeamRun。确认事务会把 TeamRun 从 ``pending`` 原子迁移为 ``running``，
        并同时写入最终配置和运行快照；事务提交后才启动 Coordinator，因此数据库中的
        TeamRun 始终是唯一执行事实源。两个并发确认请求中只有一个能够成功迁移状态。

        异常:
            ValueError: TeamRun 不存在、已处理、Team 标识或配置无效，或主 Run 不可恢复。
            KeyError: 关联的主 Agent Run 不存在。
        """

        existing = self.get(team_run_id)
        if existing is None:
            raise ValueError("Agent Team 待确认执行方案不存在")
        if existing.status != AgentTeamRunStatus.PENDING.value:
            raise ValueError("Agent Team 已确认或已处理")
        self._validate_parent_run(existing.parent_task_id, existing.parent_run_id)
        final_goal = (goal if goal is not None else existing.goal_input).strip()
        final_instructions = instructions if instructions is not None else existing.node_instructions_json
        configuration = AgentTeamConfiguration.model_validate(configuration_document)
        final_instructions = {
            key: value.strip()
            for key, value in final_instructions.items()
            if isinstance(key, str) and isinstance(value, str) and value.strip()
        }
        if not final_goal:
            raise ValueError("Team goal must not be blank")
        unknown_instructions = set(final_instructions) - {
            node.node_id for node in configuration.nodes
        }
        if unknown_instructions:
            raise ValueError("instructions reference unknown Team nodes")
        if configuration.team_id != existing.team_id:
            raise ValueError("确认配置的 team_id 与指定 TeamRun 不一致")
        preparation = self._prepare_final_plan(
            existing, configuration, goal=final_goal, instructions=final_instructions
        )

        with begin_immediate(self._session_factory) as session:
            row = self._team_run_crud.get_by_id(existing.id, session=session)
            if row.status != AgentTeamRunStatus.PENDING.value:
                raise ValueError("Agent Team 已确认或已处理")
            if row.team_id != configuration.team_id:
                raise ValueError("确认配置的 team_id 与待确认 Team 不一致")
            state = AgentTeamRunState.initial(preparation.node_runtime_snapshots)
            row.configuration_snapshot_json = configuration.model_dump(mode="json")
            row.preview_fingerprint = preparation.preview_fingerprint
            row.goal_input = final_goal
            row.node_instructions_json = final_instructions
            updated = self._team_run_crud.update_status_if_in(
                row.id,
                AgentTeamRunStatus.RUNNING.value,
                (AgentTeamRunStatus.PENDING.value,),
                state=state,
                started=True,
                session=session,
            )
            if updated is None:
                raise ValueError("Agent Team 已确认或已处理")

        from app.agent_team.coordinator import get_agent_team_coordinator

        return get_agent_team_coordinator().start(
            existing.id,
            runtime_loop=runtime_loop,
        )

    def _prepare_final_plan(
        self,
        row: AgentTeamRunModel,
        configuration: AgentTeamConfiguration,
        *,
        goal: str,
        instructions: dict[str, str],
    ) -> AgentTeamPreparationResult:
        """基于确认时的最终配置重新生成执行快照。

        该方法只读取主 Run、workspace 和运行时注册表，不写数据库。返回结果由确认事务
        统一写入 TeamRun；初始工具预览产生的候选快照不作为确认依据。
        """

        workspace_root = get_workspace_service().get_workspace(row.workspace_id).root_path
        parent_run = self._run_service.get_run(row.parent_run_id)
        fallback_model_settings = self._resolve_parent_model_settings(
            parent_run,
            workspace_root,
        )
        return AgentTeamPreparationService().prepare(
            configuration,
            goal=goal,
            instructions=instructions,
            workspace_root=workspace_root,
            parent_task_id=row.parent_task_id,
            parent_run_id=row.parent_run_id,
            workspace_id=row.workspace_id,
            fallback_model_settings=fallback_model_settings,
            fallback_model_config_id=parent_run.model_config_id,
        )

    @staticmethod
    def _resolve_parent_model_settings(
        parent_run: Any,
        workspace_root: str,
    ) -> ModelSettings | None:
        """物化主 Run 的模型设置，供确认时解析缺少独立模型配置的节点。

        主 Run 的模型连接配置来自其 ``model_config_id``，Agent profile 仅补充用户偏好。
        如果主 profile 当前不可解析，则仍返回已物化的连接设置；节点自身拥有独立模型配置
        时，准备服务可以不依赖该 fallback。
        """

        runtime_settings = None
        if parent_run.model_config_id is not None:
            runtime_settings = ModelSettings.from_model_config_record(
                get_model_config_service().get_config(parent_run.model_config_id)
            )
        agent_id = parent_run.agent_id or "main_agent"
        scope = (
            AgentProfileRegistry.SYSTEM_WORKSPACE if agent_id == "main_agent" else workspace_root
        )
        profile = get_agent_registry().resolve(scope, agent_id)
        if profile is None:
            return runtime_settings
        return profile.derive_for_run(
            parent_run,
            model_settings=runtime_settings,
        ).model_settings

    def _validate_parent_run(self, parent_task_id: int, parent_run_id: int) -> None:
        """校验 TeamRun 关联的主 Agent Run 仍属于该 Task 且可继续。"""

        parent_run = self._run_service.get_run(parent_run_id)
        if parent_run.task_id != parent_task_id:
            raise ValueError("TeamRun 不属于指定主 Agent Task")
        if parent_run.status != "waiting_for_input":
            raise ValueError("主 Agent Run 当前没有等待用户确认 TeamRun")
