"""Agent Team 执行意图与运行生命周期应用服务。

本模块把待确认和运行中的 Team 统一视为同一个持久化 TeamRun，只编排数据库状态迁移；确认后的
后台执行由 ``AgentTeamDecisionService`` 交给 Coordinator，节点调度细节不在这里，也不维护进程内
待确认缓存。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.node_goal import AgentTeamNodeGoal
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.agent_team.team_tool_error import TeamToolError
from app.config.logging.logger import log
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.service.agent_team.agent_team_execution_timeline import (
    project_node_visits,
)
from app.service.agent_team.agent_team_preparation_service import (
    AgentTeamPreparationResult,
    AgentTeamPreparationService,
)
from app.service.depends import (
    get_conversation_run_service,
    get_workspace_service,
)
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate


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

    def get_view(self, run_id: int) -> dict[str, Any]:
        """从 TeamRun、节点 Task 与 ConversationRun 事实组装前端有限读模型。"""

        row = self.get(run_id)
        state = AgentTeamRunState.model_validate(row.state_json)
        configuration = row.configuration_snapshot_json
        tasks = TaskCrud()
        runs = ConversationRunCrud()
        executions = state.node_executions
        task_ids = [execution.task_id for execution in executions.values()]
        child_tasks_by_parent = tasks.list_by_parent_task_ids(task_ids)
        run_ids = [
            run_id for execution in executions.values() for run_id in execution.conversation_run_ids
        ]
        run_ids.extend(
            task.current_run_id
            for children in child_tasks_by_parent.values()
            for task in children
            if task.current_run_id is not None
        )
        run_records = runs.get_many(list(dict.fromkeys(run_ids)))
        workspace_root = get_workspace_service().get_workspace(row.workspace_id).root_path
        visits_by_node = project_node_visits(
            state=state,
            configuration=configuration,
            team_status=row.status,
            run_records=run_records,
            child_tasks_by_parent=child_tasks_by_parent,
            workspace=workspace_root,
        )
        node_failure_reasons = {
            AgentTeamRunEndReason.NODE_RUN_FAILED.value,
            AgentTeamRunEndReason.NODE_RUN_CANCELLED.value,
            AgentTeamRunEndReason.NODE_OUTPUT_INVALID.value,
            AgentTeamRunEndReason.TRANSITION_NOT_FOUND.value,
            AgentTeamRunEndReason.NEXT_NODE_START_FAILED.value,
            AgentTeamRunEndReason.IMPLICIT_COMPLETION_MISSING.value,
        }
        failure_node_id = (
            state.current_node_id
            if row.status == AgentTeamRunStatus.FAILED.value
            and row.end_reason in node_failure_reasons
            else None
        )
        node_views: list[dict[str, Any]] = []
        for node in configuration["nodes"]:
            node_id = node["node_id"]
            visits = visits_by_node[node_id]
            if failure_node_id == node_id:
                node_status = "failed"
            elif (
                state.current_node_id == node_id
                and row.status == AgentTeamRunStatus.RUNNING.value
                and (not visits or visits[-1]["conversation_run_id"] is None)
            ):
                node_status = "running"
            elif visits:
                node_status = visits[-1]["status"]
            elif row.status in {
                AgentTeamRunStatus.COMPLETED.value,
                AgentTeamRunStatus.FAILED.value,
                AgentTeamRunStatus.CANCELLED.value,
            }:
                node_status = "skipped"
            else:
                node_status = "queued"
            node_views.append(
                {
                    "node_id": node_id,
                    "name": node["name"],
                    "agent_id": node["agent_id"],
                    "role": state.node_runtime[node_id].role or node["agent_id"],
                    "goal_summary": state.node_runtime[node_id].node_goal_summary,
                    "status": node_status,
                    "visits": visits,
                }
            )
        return {
            "id": row.id,
            "team_id": row.team_id,
            "goal": row.goal_input,
            "status": row.status,
            "end_reason": row.end_reason,
            "nodes": node_views,
            "current_node_id": (
                state.current_node_id if row.status == AgentTeamRunStatus.RUNNING.value else None
            ),
        }

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
            if (
                current.status == AgentTeamRunStatus.CANCELLED.value
                and current.end_reason == AgentTeamRunEndReason.USER_REJECTED.value
            ):
                return current
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

        不启动 Coordinator：确认在 API 边界同步完成，执行由调用方（``AgentTeamDecisionService``）
        作为后台任务交给 Coordinator。

        前端提交工具预览关联的 TeamRun ID 和用户最终配置。后端不信任预览展示数据或
        前端指纹，而是在确认边界重新校验配置、解析节点运行快照并计算指纹，再将最终
        执行计划写入 TeamRun。确认事务会把 TeamRun 从 ``pending`` 原子迁移为 ``running``，
        并同时写入最终配置和运行快照，因此数据库中的 TeamRun 始终是唯一执行事实源。两个并发
        确认请求中只有一个能够成功迁移状态；落空的一方得到 ``ValueError``，不会重复启动。

        参数:
            parent_agent: 本次确认所属主 Run 的 Agent 运行时配置（由运行时注入）。
                节点缺少独立模型连接配置时以它回落，因此必须由调用方传入已物化模型设置的副本；
                本层不再自行解析注册表（共享单例没有模型连接字段，会让准备阶段抛 ``KeyError``）。

        异常:
            ValueError: Team 标识或配置无效、TeamRun 不属于该 Task，或 TeamRun 已不在待确认态
                （已被确认、驳回或随主 Agent 取消）。
            KeyError: TeamRun 或关联的主 Agent Run 不存在。
        """

        existing = self.get(team_run_id)
        self._validate_parent_run(existing.parent_task_id, existing.parent_run_id)
        final_goal = goal.strip()
        configuration: AgentTeamConfiguration = AgentTeamConfiguration.model_validate(
            configuration_document
        )
        agent_team_preparation: AgentTeamPreparationResult = self._prepare_final_plan(
            existing,
            configuration,
            goal=final_goal,
            node_goals=node_goals,
            parent_agent=parent_agent,
        )

        with begin_immediate(self._session_factory) as session:
            if existing.team_id != configuration.team_id:
                raise ValueError("确认配置的 team_id 与待确认 Team 不一致")
            state = AgentTeamRunState.initial(
                agent_team_preparation.node_runtime_snapshots, configuration
            )
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
            raise ValueError("该 Agent Team 方案已处理，不能再确认")
        return updated

    def _prepare_final_plan(
        self,
        row: AgentTeamRunModel,
        configuration: AgentTeamConfiguration,
        *,
        goal: str,
        node_goals: dict[str, AgentTeamNodeGoal],
        parent_agent: AgentRuntimeConfig,
    ) -> AgentTeamPreparationResult:
        """基于确认时的最终配置重新生成执行快照。

        该方法只读取 workspace 与运行时注册表（节点 Profile 与工具），不写数据库。返回结果由
        确认事务统一写入 TeamRun；初始工具预览产生的候选快照不作为确认依据。

        参数:
            parent_agent: 与 :meth:`confirm` 同一份父 Agent 运行时配置，供节点
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
            parent_agent=parent_agent,
        )

    def _validate_parent_run(self, parent_task_id: int, parent_run_id: int) -> None:
        """只校验 TeamRun 关联的主 Agent Run 属于该 Task，不要求主 Run 仍在挂起。

        主 Agent 在等待审批期间可能被用户消息唤醒并完成；TeamRun 仍是 pending 时用户照样可以
        批准，结果以后台结果消息唤醒已完成的 Task。主 Agent 被用户取消时待确认 TeamRun 已随之
        取消，条件迁移会落空，不需要在这里另判主 Run 状态。
        """

        parent_run = self._run_service.get_run(parent_run_id)
        if parent_run.task_id != parent_task_id:
            raise ValueError("TeamRun 不属于指定主 Agent Task")
