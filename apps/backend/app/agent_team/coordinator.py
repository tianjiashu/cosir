"""Agent Team 节点调度、结果提交和终态收敛。"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import defaultdict
from threading import RLock
from typing import Any
from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.configuration.team_node_definition import TeamNodeDefinition
from app.agent_team.state.agent_team_run_state import AgentTeamRunState, AgentTeamNodeExecution, AgentTeamRunRuntime
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.models import ConversationRunRecord
from app.models.conversation_run_command import ConversationRunCommand
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.depends import get_runtime, get_task_service, get_conversation_run_service, \
    get_conversation_run_state_service, get_workspace_service
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory


class AgentTeamCoordinator:
    """拥有 TeamRun 的节点调度状态机，并把单节点执行委托给 ConversationRunExecutor。

    每条 Team 由一个异步驱动任务串行执行节点：Coordinator 等待节点 Run 结束、读取其
    持久化 ``final_output``、结算 transition 并创建下一节点。Executor 只执行单个 Run；主
    ConversationRun 的等待与恢复交给 ``AgentTeamParentRunService``。
    """

    def __init__(self) -> None:

        from app.config.configuration import get_tool_system
        from app.service.agent_team.agent_team_run_service import get_agent_registry

        self._team_run_crud = AgentTeamRunCrud()
        self._task_service = get_task_service()
        self._tool_system = get_tool_system()
        self._run_service = get_conversation_run_service()
        self._run_state_service = get_conversation_run_state_service()
        self._session_factory = main_session_factory()
        self._locks_guard = RLock()
        self._locks: dict[int, RLock] = defaultdict(RLock)
        self._runtime_loops_guard = RLock()
        self._runtime_loops: dict[int, asyncio.AbstractEventLoop] = {}
        self._runner = get_runtime()
        self._workspace_service = get_workspace_service()
        self._agent_registry = get_agent_registry()

    def start(
            self,
            team_run: AgentTeamRunModel,
    ) -> AgentTeamRunModel:
        """启动已确认的 TeamRun，并登记由 Coordinator 拥有的节点驱动任务。

        本方法只接受已经持久化且完成确认的 ``running`` TeamRun，不读取待确认请求、预览
        文档或用户确认参数。TeamRun 的创建、确认和 ``pending`` 到 ``running`` 的原子迁移
        由执行请求 service 完成；本方法持久化入口节点后，启动一个异步任务串行驱动整条 Team。

        返回:
            已创建入口节点后的 TeamRun 快照；后续节点由异步驱动任务继续调度。

        异常:
            KeyError: TeamRun 不存在。
            ValueError: Team 已处于非运行态或已有活动节点（重复启动），或已冻结的节点配置或
                运行快照无效。
        """
        state: AgentTeamRunState | None = None
        try:
            if team_run.status != AgentTeamRunStatus.RUNNING.value:
                raise ValueError(f"Team 已进入非运行态({team_run.status})，无法启动")
            state = AgentTeamRunState.model_validate(team_run.state_json)
            configuration = AgentTeamConfiguration.model_validate(
                team_run.configuration_snapshot_json
            )

            state.current_node_id = configuration.start_node_id
            next_node_input = None
            rounds = 0

            while True:
                if rounds >= configuration.max_runs:
                    self._team_run_crud.update_status_if_in(team_run.id, AgentTeamRunStatus.FAILED.value,
                                                            (AgentTeamRunStatus.RUNNING.value,), state=state,
                                                            end_reason=AgentTeamRunEndReason.MAX_RUNS_EXCEEDED.value,
                                                            ended=True)
                    log.error(
                        "agent_team_max_runs_exceeded",
                        extra={
                            "msg": "Agent Team 超过最大轮数，已收敛为失败",
                            "data": {"run_id": team_run.id, "max_runs": configuration.max_runs},
                        },
                    )
                    break
                rounds += 1
                row = self._team_run_crud.update_status_if_in(team_run.id, AgentTeamRunStatus.RUNNING.value,
                                                                (AgentTeamRunStatus.RUNNING.value,), state=state)
                if row is None:
                    log.info(
                        "agent_team_start_failed",
                        extra={
                            "msg": "Agent Team 已被取消",
                            "data": {"run_id": team_run.id},
                        },
                    )
                    break

                node_execution, node_run = self._start_node(
                    team_run,
                    state,
                    configuration,
                    next_node_input,
                )
                if node_run.status != ConversationRunStatus.COMPLETED.value:
                    log.info(
                        "agent_team_start_failed",
                        extra={
                            "msg": "Agent Team 节点执行失败",
                            "data": {
                                "run_id": team_run.id,
                                "node_id": state.current_node_id,
                                "output": node_run.final_output,
                            },
                        },
                    )
                    break

                final_output = node_run.final_output
                status, next_node_input = self.struct_out_put(final_output)
                next_node_id = state.has_next_node(status)
                if next_node_id is None:
                    log.error(
                        "agent_team_start_failed",
                        extra={
                            "msg": "next_node_id 为空",
                            "data": {
                                "run_id": team_run.id,
                                "node_id": state.current_node_id,
                                "output": final_output,
                            },
                        },
                    )
                    self._team_run_crud.update_status_if_in(team_run.id, AgentTeamRunStatus.FAILED.value,
                                                            (AgentTeamRunStatus.RUNNING.value,), state=state,
                                                            end_reason="next_node_id 为空", ended=True)
                    break
                if next_node_id == "END":
                    log.info(
                        "agent_team_start_completed",
                        extra={
                            "msg": "Agent Team 已完成",
                            "data": {
                                "run_id": team_run.id,
                                "node_id": state.current_node_id,
                                "output": final_output,
                            },
                        },
                    )
                    final_node_output = next_node_input
                    self._team_run_crud.update_status_if_in(team_run.id, AgentTeamRunStatus.COMPLETED.value,
                                                            (AgentTeamRunStatus.RUNNING.value,), state=state,
                                                            end_reason=final_node_output, ended=True)
                    break
                state.add_transition(state.current_node_id, status, next_node_id)
                state.current_node_id = next_node_id
        except Exception as exc:
            log.exception(
                "agent_team_start_failed",
                extra={
                    "msg": "Agent Team 入口节点启动失败",
                    "data": {
                        "run_id": team_run.id,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            self._team_run_crud.update_status_if_in(team_run.id, AgentTeamRunStatus.FAILED.value,
                                                    (AgentTeamRunStatus.RUNNING.value,), state=state,
                                                    end_reason="next_node_id 为空", ended=True)
            raise
        return self._team_run_crud.get_by_id(team_run.id)

    def struct_out_put(self, output: str) -> tuple[str, str] | None:
        try:
            json_output: dict[str, Any] = json.loads(output)
            status = json_output.get("status")
            output = json_output.get("output")
            if status is not None and output is not None:
                return status, output
            raise ValueError("Invalid output")
        except Exception:
            return None

    def _start_node(
            self,
            row: AgentTeamRunModel,
            state: AgentTeamRunState,
            configuration: AgentTeamConfiguration,
            node_input: str | None = None,
    ) -> tuple[AgentTeamNodeExecution, ConversationRunRecord]:
        """在调用方事务中复用或创建节点 Task，并创建本次节点 Run（只写执行事实）。

        本方法是「可信调用方」的写助手：``row`` / ``state`` / ``configuration`` 由调用方在
        自身事务中加载并校验——``start`` 在入口统一校验 Team 存在、处于 running 且无活动节点；
        驱动循环在结算时按锁定后的快照校验。本方法不再读取或重新校验 Team 身份与生命周期
        状态，只负责按节点快照派生 Task/Run 并落定执行记录。``state.start_node`` 仍持有活动
        节点游标的领域守卫。

        同一 TeamRun 中的节点按 ``node_id`` 绑定独立 Task；再次访问该节点时读取原 Task，
        由 Task 级 context 保留它自己的历史。节点事件发布和 Run 启动由 Coordinator 驱动循环
        在事务提交后执行。
        """
        node_id = state.current_node_id
        node: TeamNodeDefinition = configuration.node(node_id)
        node_runtime: AgentTeamRunRuntime = state.node_runtime.get(node_id)
        agent_id = node.agent_id
        tool_schemas = self._resolve_node_tool_schemas(node_runtime.allowed_tools)
        node_execution: AgentTeamNodeExecution | None = state.node_execution_for_node(node_id)
        node_goal = node_runtime.node_goal
        input_text = self._build_node_input(
            node_goal,
            node_input,
        )
        model_settings = node_runtime.model_settings
        reasoning_effort = model_settings.get("reasoning_effort")

        if node_execution is None:
            task = self._task_service.get_or_create_task(
                workspace_id=row.workspace_id,
                title=node.name,
                task_type=f"agent_team_node",
                parent_task_id=row.parent_task_id,
                parent_run_id=row.parent_run_id,
                extra={
                    "agent_team_run_id": row.id,
                    "agent_team_node_id": node_id,
                    "agent_team_profile_snapshot": node_runtime.model_dump(mode="json"),
                },
                tool_schemas=tool_schemas,
            )
            node_execution: AgentTeamNodeExecution = state.create_execution_node(node_id,
                                                                                 AgentTeamNodeExecution(node_id=node_id,
                                                                                                        task_id=task.id))

        node_run = self._run_service.create_run(
            task_id=node_execution.task_id,
            agent_id=agent_id,
            status=ConversationRunStatus.RUNNING.value,
            model_config_id=node_runtime.model_config_id,
            reasoning_effort=reasoning_effort,
            run_command=ConversationRunCommand(display_text=input_text)
        )
        node_execution.conversation_run_ids.append(node_run.id)
        workspace_root = self._workspace_root(row.workspace_id)
        agent_profile: AgentProfile = self._agent_registry.resolve(workspace_root, agent_id)

        row = self._team_run_crud.update_status_if_in(row.id, AgentTeamRunStatus.RUNNING.value,
                                                (AgentTeamRunStatus.RUNNING.value,), state=state)
        if row is None:
            log.info(
                "agent_team_start_failed",
                extra={
                    "msg": "Agent Team 已被取消",
                    "data": {"run_id": row.id},
                },
            )
            return node_execution, node_run


        asyncio.run(self._runner.run_agent(AgentProfile(
            agent_id=agent_id,
            role=agent_profile.role,
            system_prompt=node_runtime.system_prompt,
            allowed_tools=node_runtime.allowed_tools,
            agent_type=agent_profile.agent_type,
            model_config_id=agent_profile.model_config_id,
            model_settings=agent_profile.model_settings,
            structured_output=agent_profile.structured_output,
            max_steps=agent_profile.max_steps,
            run=node_run
        )))
        node_run = self._run_service.get_run(node_run.id)

        return node_execution, node_run

    def _resolve_node_tool_schemas(self, allowed_tools: list[str]) -> list[dict[str, object]]:
        """按节点快照声明的 ``allowed_tools`` 名字，从当前工具注册表解析冻结 schema。

        节点运行快照只持久化工具名（``allowed_tools``），工具 schema 在节点启动时按名字
        从进程级 ToolSystem 实时解析并固化进 Task。这样快照不必携带易漂移的 schema 契约，
        也保证节点工具集合与确认时一致（工具名冻结于快照）。

        参数:
            allowed_tools: 节点允许使用的工具名列表。

        返回:
            与运行期模型 schema 同源的 JSON 字典列表，可直接传给
            ``TaskService.get_or_create_task`` 的 ``tool_schemas``。

        异常:
            ValueError: 某个工具名在当前注册表中不存在（可能已在确认后从 workspace 移除）。
        """
        registered = {tool.name: tool for tool in self._tool_system.executor.list_tools()}
        schemas: list[dict[str, object]] = []
        for name in allowed_tools:
            tool = registered.get(name)
            if tool is None:
                raise ValueError(
                    f"Team 节点声明的工具 '{name}' 不在当前工具注册表中，"
                    f"无法解析其 schema（可能已在确认后从 workspace 移除）"
                )
            schemas.append(copy.deepcopy(tool.to_model_tool_definition()))
        return schemas

    @staticmethod
    def _build_node_input(node_goal: str, node_input: str | None = None) -> str:
        """只把当前节点子目标和直接前置输出放入 user input；全局目标在 system prompt。"""

        sections = []
        if node_goal:
            sections.append(f"本节点子目标:\n{node_goal}")
        if node_input:
            sections.append("前置节点输出:\n" + node_input)
        return "\n\n".join(sections) or "请依据当前节点职责推进 Team 总目标。"

    def _workspace_root(self, workspace_id: int) -> str:
        """读取 workspace 根路径。"""

        return self._workspace_service.get_workspace(workspace_id).root_path

    def cancel(self, team_run_id: int) -> AgentTeamRunModel | None:

        row = self._team_run_crud.get_by_id(team_run_id)
        if row.status not in {"pending", "running"}:
            return row
        state = AgentTeamRunState.model_validate(row.state_json)

        row = self._team_run_crud.update_status_if_in(row.id, AgentTeamRunStatus.CANCELLED.value,
                                                      (AgentTeamRunStatus.RUNNING.value,))
        if row is None:
            return row

        state.cancel()
        return row

    def cancel_for_task_ids(self, task_ids: set[int]) -> int:
        """在删除任务树前取消与其关联的活动 Team。

        参数:
            task_ids: 即将删除的任务及其子任务标识集合。

        返回:
            实际从活动态收敛为 ``cancelled`` 的 Team 数量。

        异常:
            KeyError: TeamRun 在扫描后被其他路径删除时向上暴露，调用方应中止删除；
            数据库或执行器异常：向上暴露，避免删除任务后留下仍在执行的节点。

        副作用:
            取消以这些任务作为主 Agent 的 Team，以及节点 Task 位于集合中的 Team；
            同步发出节点取消信号。
        """

        if not task_ids:
            return 0
        cancelled = 0
        for row in (
                *self._team_run_crud.list_pending_confirmations(),
                *self._team_run_crud.list_active(),
        ):
            team = self.cancel(row.id)
            if team is not None:
                cancelled += 1
        return cancelled

    def cancel_for_parent_run_id(self, parent_run_id: int) -> int:
        """取消指定主 Agent Run 创建的所有活动 Team。

        该方法由 ConversationRunExecutor 的显式取消入口调用，负责把主 Run 的取消信号
        传播到待确认或运行中的 Team 及其当前节点 Run。

        参数:
            parent_run_id: 主 Agent ConversationRun 标识。

        返回:
            实际从活动态收敛为 ``cancelled`` 的 Team 数量。

        副作用:
            更新 TeamRun 终态并取消节点执行器；异常向调用方暴露，供
            取消入口记录诊断日志，但不回滚主 Run 已发出的取消信号。
        """

        cancelled = 0
        for row in (
                *self._team_run_crud.list_pending_confirmations(),
                *self._team_run_crud.list_active(),
        ):
            if row.parent_run_id != parent_run_id:
                continue
            team = self.cancel(row.id)
            if team is not None:
                cancelled += 1
        return cancelled

    def recover_after_restart(self) -> int:
        """把后端重启遗留的 Team 运行和未确认方案收敛为 cancelled，不自动重放。"""

        pending = self._team_run_crud.list_pending_confirmations()
        active = self._team_run_crud.list_active()
        recovered = 0
        for row in (*pending, *active):
            previous_status = row.status
            updated = self._team_run_crud.update_status_if_in(
                row.id,
                AgentTeamRunStatus.CANCELLED.value,
                (previous_status,),
                end_reason=AgentTeamRunEndReason.RUNTIME_RESTARTED.value,
                ended=True,
            )
            if updated is None:
                continue
            recovered += 1
        return recovered

    def get(self, team_run_id: int) -> AgentTeamRunModel:
        """读取 TeamRun 当前持久化快照。"""

        return self._team_run_crud.get_by_id(team_run_id)


_COORDINATOR: AgentTeamCoordinator | None = None


def get_agent_team_coordinator() -> AgentTeamCoordinator:
    """返回进程级 Team coordinator。"""

    global _COORDINATOR
    if _COORDINATOR is None:
        _COORDINATOR = AgentTeamCoordinator()
    return _COORDINATOR
