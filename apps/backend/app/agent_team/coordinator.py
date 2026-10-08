"""Agent Team 节点调度、结果提交和终态收敛。"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import defaultdict
from concurrent.futures import Future
from threading import RLock
from typing import Any

from sqlalchemy.orm import Session

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.configuration.team_node_definition import TeamNodeDefinition
from app.agent_team.state.agent_team_run_state import AgentTeamRunState, AgentTeamNodeExecution, AgentTeamRunRuntime
from app.assistant_transport.event import RunInitializedEvent, RunStatusChangedEvent
from app.assistant_transport.event.dispatch import dispatch_conversation_event
from app.config.configuration import get_tool_system
from app.config.logging.logger import log
from app.models import ConversationRunRecord
from app.models.conversation_run_command import ConversationRunCommand
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service import depends as service_depends
from app.service.agent_team.agent_team_parent_run_service import (
    AgentTeamParentRunService,
)
from app.service.conversation_run.conversation_run_service import ConversationRunService
from app.service.conversation_run.conversation_run_state_service import ConversationRunStateService
from app.service.depends import get_runtime, get_task_service, get_conversation_run_service, \
    get_conversation_run_state_service
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate
from app.task_runtime.service.task_service import TaskService


class AgentTeamCoordinator:
    """拥有 TeamRun 的节点调度状态机，并把单节点执行委托给 ConversationRunExecutor。

    每条 Team 由一个异步驱动任务串行执行节点：Coordinator 等待节点 Run 结束、读取其
    持久化 ``final_output``、结算 transition 并创建下一节点。Executor 只执行单个 Run；主
    ConversationRun 的等待与恢复交给 ``AgentTeamParentRunService``。
    """

    def __init__(self) -> None:
        self._team_run_crud = AgentTeamRunCrud()
        self._task_service = get_task_service()
        self._tool_system = get_tool_system()
        self._configurations: dict[int, AgentTeamConfiguration] = {}
        self._run_service = get_conversation_run_service()
        self._run_state_service = get_conversation_run_state_service()
        self._parent_run_service = AgentTeamParentRunService()
        self._session_factory = main_session_factory()
        self._locks_guard = RLock()
        self._locks: dict[int, RLock] = defaultdict(RLock)
        self._runtime_loops_guard = RLock()
        self._runtime_loops: dict[int, asyncio.AbstractEventLoop] = {}
        self.runner = get_runtime()

    def _lock_for(self, team_run_db_id: int) -> RLock:
        with self._locks_guard:
            return self._locks[team_run_db_id]

    def start(
            self,
            team_run_id: int,
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

        row: AgentTeamRunModel | None = None
        try:
            with begin_immediate(self._session_factory) as session:
                row = session.get(AgentTeamRunModel, team_run_id)
                if row is None:
                    raise KeyError(team_run_id)
                if row.status != AgentTeamRunStatus.RUNNING.value:
                    raise ValueError(f"Team 已进入非运行态({row.status})，无法启动")
                state = AgentTeamRunState.model_validate(row.state_json)
                configuration = AgentTeamConfiguration.model_validate(
                    row.configuration_snapshot_json
                )
                self._configurations[row.id] = configuration

                node_id = configuration.start_node_id

                node_execution, node_run = self._start_node(
                    row,
                    state,
                    configuration,
                    node_id,
                    session=session,
                )

        except Exception as exc:
            if row is not None:
                self._fail_team(row.id, AgentTeamRunEndReason.TEAM_START_FAILED)
            log.exception(
                "agent_team_start_failed",
                extra={
                    "msg": "Agent Team 入口节点启动失败",
                    "data": {
                        "run_id": team_run_id,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            raise
        return self._team_run_crud.get_by_id(row.id)

    def _start_node(
            self,
            row: AgentTeamRunModel,
            state: AgentTeamRunState,
            configuration: AgentTeamConfiguration,
            node_id: str,
            *,
            session: Session,
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

        node: TeamNodeDefinition = configuration.node(node_id)
        node_runtime: AgentTeamRunRuntime = state.node_runtime.get(node_id)
        agent_id = node.agent_id
        tool_definitions = self._resolve_node_tool_definitions(node_runtime.allowed_tools)
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
                tool_definitions=tool_definitions,
                session=session,
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
            run_command=ConversationRunCommand(display_text=input_text),
            session=session,
        )
        node_execution.conversation_run_ids.append(node_run.id)
        self.runner.execute_run(node_run)
        node_run = self._run_service.get_run(node_run.id)

        return node_execution, node_run

    def _resolve_node_tool_definitions(self, allowed_tools: list[str]) -> list[dict[str, object]]:
        """按节点快照声明的 ``allowed_tools`` 名字，从当前工具注册表解析冻结 schema。

        节点运行快照只持久化工具名（``allowed_tools``），工具 schema 在节点启动时按名字
        从进程级 ToolSystem 实时解析并固化进 Task。这样快照不必携带易漂移的 schema 契约，
        也保证节点工具集合与确认时一致（工具名冻结于快照）。

        参数:
            allowed_tools: 节点允许使用的工具名列表。

        返回:
            与运行期模型 schema 同源的 JSON 字典列表，可直接传给
            ``TaskService.get_or_create_task`` 的 ``tool_definitions``。

        异常:
            ValueError: 某个工具名在当前注册表中不存在（可能已在确认后从 workspace 移除）。
        """
        if self._tool_system is None:
            self._tool_system = get_tool_system()
        registered = {tool.name: tool for tool in self._tool_system.executor.list_tools()}
        definitions: list[dict[str, object]] = []
        for name in allowed_tools:
            tool = registered.get(name)
            if tool is None:
                raise ValueError(
                    f"Team 节点声明的工具 '{name}' 不在当前工具注册表中，"
                    f"无法解析其 schema（可能已在确认后从 workspace 移除）"
                )
            definitions.append(copy.deepcopy(tool.to_model_tool_definition()))
        return definitions


    @staticmethod
    def _build_node_input(node_goal: str, node_input: str | None = None) -> str:
        """只把当前节点子目标和直接前置输出放入 user input；全局目标在 system prompt。"""

        sections = []
        if node_goal:
            sections.append(f"本节点子目标:\n{node_goal}")
        if node_input:
            sections.append("前置节点输出:\n" + node_input)
        return "\n\n".join(sections) or "请依据当前节点职责推进 Team 总目标。"

    @staticmethod
    def _workspace_root(workspace_id: int) -> str:
        """读取 workspace 根路径。"""

        return service_depends.get_workspace_service().get_workspace(workspace_id).root_path

    def _fail_team(
            self,
            team_run_db_id: int,
            end_reason: AgentTeamRunEndReason,
            *,
            state: AgentTeamRunState | None = None,
    ) -> None:
        """把 Team 收敛为失败；节点已结算时同时保留其状态快照。"""

        self._configurations.pop(team_run_db_id, None)
        current = self._team_run_crud.get_by_id(team_run_db_id)
        if state is None:
            state = AgentTeamRunState.model_validate(current.state_json)
        completed_runs = state.completed_run_ids()
        row = self._team_run_crud.update_status_if_in(
            team_run_db_id,
            AgentTeamRunStatus.FAILED.value,
            (AgentTeamRunStatus.RUNNING.value,),
            state=state,
            end_reason=end_reason.value,
            ended=True,
        )
        if row is None:
            log.info(
                "agent_team_stale_failure_ignored",
                extra={
                    "msg": "忽略已不处于运行态的 Team 失败收敛",
                    "data": {"team_run_db_id": team_run_db_id, "end_reason": end_reason.value},
                },
            )
            return
        for _, run_id in state.node_references():
            if run_id not in completed_runs:
                self._cancel_node_execution(run_id, current.id, end_reason="agent_team_failed")

    def cancel(self, team_run_id: int) -> AgentTeamRunModel:
        """取消 Team 和当前活动节点 Run。"""

        row = self._team_run_crud.get_by_id(team_run_id)
        with self._lock_for(team_run_id):
            row = self._team_run_crud.get_by_id(row.id)
            if row.status not in {"pending", "running"}:
                return row
            result = self._team_run_crud.update_status_if_in(
                row.id,
                "cancelled",
                ("pending", "running"),
                end_reason=AgentTeamRunEndReason.CANCELLED.value,
                ended=True,
            )
            if result is None:
                return self._team_run_crud.get_by_id(row.id)
            state = AgentTeamRunState.model_validate(row.state_json)
            for _, node_run_id in state.node_references():
                self._cancel_node_execution(node_run_id, row.id, end_reason="agent_team_cancelled")
            return result

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
            state = AgentTeamRunState.model_validate(row.state_json)
            node_task_ids = {task_id for task_id, _ in state.node_references()}
            if row.parent_task_id not in task_ids and not node_task_ids.intersection(task_ids):
                continue
            previous = row.status
            self.cancel(row.id)
            if previous in {"pending", "running"}:
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
            previous = row.status
            self.cancel(row.id)
            if previous in {"pending", "running"}:
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


    def _runtime_loop_for(self, team_run_id: int) -> asyncio.AbstractEventLoop | None:
        """读取 Team 绑定的本地运行时事件循环。"""

        with self._runtime_loops_guard:
            return self._runtime_loops.get(team_run_id)

    def _cancel_node_execution(
            self, node_run_id: int, team_run_id: int, *, end_reason: str
    ) -> None:
        """通过现有执行器发出取消信号，并同步落定尚未结束的节点 Run。"""

        runtime_loop = self._runtime_loop_for(team_run_id)
        if runtime_loop is not None and not runtime_loop.is_closed():
            try:
                from app.service.depends import get_conversation_run_executor

                future = asyncio.run_coroutine_threadsafe(
                    get_conversation_run_executor().cancel(node_run_id, end_reason=end_reason),
                    runtime_loop,
                )
                future.result(timeout=10)
            except Exception as exc:
                log.warning(
                    "agent_team_node_executor_cancel_failed",
                    extra={
                        "msg": "Agent Team 节点执行器取消信号发送失败，继续执行数据库收敛",
                        "data": {
                            "run_id": team_run_id,
                            "node_run_id": node_run_id,
                            "error_type": type(exc).__name__,
                        },
                    },
                )
        self._run_state_service.cancel_run_if_running(node_run_id, end_reason=end_reason)

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
