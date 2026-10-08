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
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.assistant_transport.event import RunInitializedEvent, RunStatusChangedEvent
from app.assistant_transport.event.dispatch import dispatch_conversation_event
from app.config.configuration import get_tool_system
from app.config.logging.logger import log
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

    def __init__(self, tool_system: Any | None = None) -> None:
        self._team_run_crud = AgentTeamRunCrud()
        self._task_service = TaskService()
        self._tool_system = tool_system
        self._configurations: dict[int, AgentTeamConfiguration] = {}
        self._run_service = ConversationRunService()
        self._run_state_service = ConversationRunStateService()
        self._parent_run_service = AgentTeamParentRunService(self._run_service)
        self._session_factory = main_session_factory()
        self._locks_guard = RLock()
        self._locks: dict[int, RLock] = defaultdict(RLock)
        self._runtime_loops_guard = RLock()
        self._runtime_loops: dict[int, asyncio.AbstractEventLoop] = {}
        self._team_drivers_guard = RLock()
        self._team_drivers: dict[int, Future[Any]] = {}

    def _lock_for(self, team_run_db_id: int) -> RLock:
        with self._locks_guard:
            return self._locks[team_run_db_id]

    def start(
        self,
        team_run_id: int,
        *,
        runtime_loop: asyncio.AbstractEventLoop,
    ) -> AgentTeamRunModel:
        """启动已确认的 TeamRun，并登记由 Coordinator 拥有的节点驱动任务。

        本方法只接受已经持久化且完成确认的 ``running`` TeamRun，不读取待确认请求、预览
        文档或用户确认参数。TeamRun 的创建、确认和 ``pending`` 到 ``running`` 的原子迁移
        由执行请求 service 完成；本方法持久化入口节点后，启动一个异步任务串行驱动整条 Team。

        返回:
            已创建入口节点后的 TeamRun 快照；后续节点由异步驱动任务继续调度。

        异常:
            KeyError: TeamRun 不存在。
            ValueError: 已冻结的节点配置或运行快照无效。
            RuntimeError: 入口节点未能创建 ConversationRun。
        """

        initial = self._team_run_crud.get_by_id(team_run_id)
        row: AgentTeamRunModel | None = None
        try:
            with begin_immediate(self._session_factory) as session:
                row = session.get(AgentTeamRunModel, initial.id)
                if row is None:
                    raise KeyError(team_run_id)
                configuration = AgentTeamConfiguration.model_validate(
                    row.configuration_snapshot_json
                )
                self._configurations[row.id] = configuration
                task, node_run = self._start_node(
                    row.id,
                    configuration.start_node_id,
                    configuration=configuration,
                    session=session,
                )
            with self._runtime_loops_guard:
                self._runtime_loops[row.id] = runtime_loop
            self._publish_node_started(task, node_run)
            driver = asyncio.run_coroutine_threadsafe(
                self._drive_team(row.id, node_run.id),
                runtime_loop,
            )
            with self._team_drivers_guard:
                self._team_drivers[row.id] = driver
            driver.add_done_callback(lambda completed: self._forget_team_driver(row.id, completed))
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

    async def _drive_team(self, team_run_id: int, node_run_id: int) -> None:
        """顺序执行 Team 节点，并在 Team 终态后恢复主 Run。

        Executor task 结束后才读取 Run 的 canonical 状态与 ``final_output``；节点间的
        结果解析、状态结算和下一节点创建均由 Coordinator 完成。
        """

        try:
            while True:
                team = await asyncio.to_thread(self._team_run_crud.get_by_id, team_run_id)
                if team.status != AgentTeamRunStatus.RUNNING.value:
                    break

                from app.service.depends import get_conversation_run_executor

                execution = await get_conversation_run_executor().start(
                    node_run_id,
                    start_mode="fresh",
                )
                await asyncio.shield(execution)

                team = await asyncio.to_thread(self._team_run_crud.get_by_id, team_run_id)
                if team.status != AgentTeamRunStatus.RUNNING.value:
                    break
                node_run = await asyncio.to_thread(self._run_service.get_run, node_run_id)
                if node_run.status != ConversationRunStatus.COMPLETED.value:
                    reason = {
                        ConversationRunStatus.FAILED.value: AgentTeamRunEndReason.NODE_RUN_FAILED,
                        ConversationRunStatus.CANCELLED.value: (
                            AgentTeamRunEndReason.NODE_RUN_CANCELLED
                        ),
                    }.get(
                        node_run.status,
                        AgentTeamRunEndReason.IMPLICIT_COMPLETION_MISSING,
                    )
                    await asyncio.to_thread(self._fail_team, team_run_id, reason)
                    break

                try:
                    result = json.loads(node_run.final_output or "")
                    if not isinstance(result, dict) or set(result) != {"status", "output"}:
                        raise ValueError("结构化结果必须且只能包含 status 和 output")
                    status = result["status"]
                    output = result["output"]
                    if not isinstance(status, str) or not isinstance(output, str):
                        raise ValueError("结构化结果的 status 和 output 必须是字符串")
                    next_node = await asyncio.to_thread(
                        self._settle_node_result,
                        node_run_id,
                        status,
                        output,
                    )
                except (json.JSONDecodeError, TypeError, ValueError):
                    log.exception(
                        "agent_team_node_output_invalid",
                        extra={
                            "msg": "Agent Team 节点完成结果不符合结构化输出契约",
                            "data": {"team_run_id": team_run_id, "node_run_id": node_run_id},
                        },
                    )
                    await asyncio.to_thread(
                        self._fail_team,
                        team_run_id,
                        AgentTeamRunEndReason.NODE_OUTPUT_INVALID,
                    )
                    break

                if next_node is None:
                    break
                task, node_run = next_node
                await asyncio.to_thread(self._publish_node_started, task, node_run)
                node_run_id = node_run.id

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "agent_team_driver_failed",
                extra={
                    "msg": "Agent Team Coordinator 驱动失败",
                    "data": {"team_run_id": team_run_id, "node_run_id": node_run_id},
                },
            )
            await asyncio.to_thread(
                self._fail_team,
                team_run_id,
                AgentTeamRunEndReason.RUNTIME_UNAVAILABLE,
            )

        team = await asyncio.to_thread(self._team_run_crud.get_by_id, team_run_id)
        if team.status not in {
            AgentTeamRunStatus.COMPLETED.value,
            AgentTeamRunStatus.FAILED.value,
            AgentTeamRunStatus.CANCELLED.value,
        }:
            return
        try:
            resumed = await self._parent_run_service.resume_parent_after_team(
                team,
                self._build_team_result_message(team),
            )
            if resumed:
                log.info(
                    "agent_team_parent_run_resumed",
                    extra={
                        "msg": "Agent Team 结果已交给主 Agent，并恢复其等待中的 Run",
                        "data": {
                            "team_run_id": team_run_id,
                            "parent_task_id": team.parent_task_id,
                            "parent_run_id": team.parent_run_id,
                            "team_status": team.status,
                        },
                    },
                )
        except Exception:
            log.exception(
                "agent_team_parent_run_resume_failed",
                extra={
                    "msg": "Agent Team 结束后恢复主 Agent Run 失败",
                    "data": {"team_run_id": team_run_id},
                },
            )

    def _forget_team_driver(self, team_run_id: int, completed: Future[Any]) -> None:
        """移除结束的 Team 驱动任务及其运行期索引。"""

        with self._team_drivers_guard:
            if self._team_drivers.get(team_run_id) is completed:
                self._team_drivers.pop(team_run_id, None)
        self._configurations.pop(team_run_id, None)
        with self._runtime_loops_guard:
            self._runtime_loops.pop(team_run_id, None)

    @staticmethod
    def _build_team_result_message(row: AgentTeamRunModel) -> str:
        """把 TeamRun 聚合结果转换成主 Agent 可消费的结构化文本。"""

        state = AgentTeamRunState.model_validate(row.state_json)
        active_execution = state.active_execution()
        return "Agent Team 已结束。以下是 TeamResult，请基于它继续处理原始目标：\n" + json.dumps(
            {
                "run_id": row.id,
                "team_id": row.team_id,
                "status": row.status,
                "goal": row.goal_input,
                "active_node_id": active_execution.node_id if active_execution else None,
                "node_results": [
                    execution.model_dump(mode="json")
                    for execution in state.node_executions
                    if execution.completed
                ],
                "end_reason": row.end_reason,
            },
            ensure_ascii=False,
        )

    def _start_node(
        self,
        team_run_db_id: int,
        node_id: str,
        configuration: AgentTeamConfiguration,
        *,
        session: Session,
    ) -> tuple[Any, Any]:
        """在调用方事务中复用或创建节点 Task，并创建本次节点 Run。

        同一 TeamRun 中的节点按 ``node_id`` 绑定独立 Task；再次访问该节点时读取原 Task，
        由 Task 级 context 保留它自己的历史。Task 映射、Run 创建和节点执行记录在调用方
        事务中一起提交。此方法只写入执行事实，节点事件发布和 Run 启动由 Coordinator 驱动循环
        在事务提交后执行。
        """

        row = session.get(AgentTeamRunModel, team_run_db_id)
        if row is None:
            raise KeyError(team_run_db_id)
        state = AgentTeamRunState.model_validate(row.state_json)
        node = configuration.node(node_id)
        runtime_snapshot = state.runtime.node_snapshots.get(node_id)
        if not isinstance(runtime_snapshot, dict):
            raise ValueError(f"Team 节点缺少已冻结的运行快照: {node_id}")
        agent_id = runtime_snapshot.get("agent_id")
        allowed_tools = runtime_snapshot.get("allowed_tools")
        if not isinstance(agent_id, str) or not isinstance(allowed_tools, list):
            raise ValueError(f"Team 节点运行快照无效: {node_id}")
        tool_definitions = self._resolve_node_tool_definitions(
            [str(tool) for tool in allowed_tools]
        )
        if state.active_node_run_id is not None:
            raise ValueError("Team 已存在尚未完成的活动节点")
        node_task_id = state.task_id_for_node(node_id)
        node_goal = runtime_snapshot["node_goal"] if node_task_id is None else ""
        previous_outputs = self._previous_outputs_for_node(configuration, state, node_id)
        input_text = self._build_node_input(
            node_goal,
            previous_outputs,
        )
        parent_run = self._run_service.get_run(row.parent_run_id)
        model_settings = runtime_snapshot.get("model_settings", {})
        reasoning_effort = model_settings.get("reasoning_effort")

        if node_task_id is None:
            task = self._task_service.get_or_create_task(
                workspace_id=row.workspace_id,
                title=node.name,
                task_type="agent_team_node",
                parent_task_id=row.parent_task_id,
                parent_run_id=row.parent_run_id,
                extra={
                    "agent_team_run_id": row.id,
                    "agent_team_node_id": node_id,
                    "agent_team_profile_snapshot": runtime_snapshot,
                },
                tool_definitions=tool_definitions,
                session=session,
            )
        else:
            task = self._task_service.get_or_create_task(
                workspace_id=row.workspace_id,
                title=node.name,
                task_id=node_task_id,
                session=session,
            )
        state.bind_node_task(node_id, task.id)
        node_run = self._run_service.create_run(
            task_id=task.id,
            agent_id=agent_id,
            status=ConversationRunStatus.RUNNING.value,
            model_config_id=runtime_snapshot.get("model_config_id") or parent_run.model_config_id,
            reasoning_effort=reasoning_effort,
            run_command=ConversationRunCommand(display_text=input_text),
            session=session,
        )
        state.start_node(node_id, task.id, node_run.id)
        updated = self._team_run_crud.update_status_if_in(
            team_run_db_id,
            AgentTeamRunStatus.RUNNING.value,
            (AgentTeamRunStatus.RUNNING.value,),
            state=state,
            session=session,
        )
        if updated is None:
            raise RuntimeError("TeamRun 已被其他路径处理，无法启动节点")
        return task, node_run

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

    def _publish_node_started(self, task: Any, node_run: Any) -> None:
        """在节点事实事务提交后发布初始化和 running 快照事件。"""

        projector = service_depends.get_conversation_event_projector()
        projector.process(
            RunInitializedEvent(
                task_id=task.id,
                run_id=node_run.id,
                context_window_total=self._task_service.get_task(task.id).context_window_total,
            )
        )
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=task.id,
                run_id=node_run.id,
                status=ConversationRunStatus.RUNNING,
            )
        )

    @staticmethod
    def _build_node_input(node_goal: str, previous_outputs: list[dict[str, Any]]) -> str:
        """只把当前节点子目标和直接前置输出放入 user input；全局目标在 system prompt。"""

        sections = []
        if node_goal:
            sections.append(f"本节点子目标:\n{node_goal}")
        if previous_outputs:
            sections.append("前置节点输出:\n" + json.dumps(previous_outputs, ensure_ascii=False))
        return "\n\n".join(sections) or "请依据当前节点职责推进 Team 总目标。"

    @staticmethod
    def _previous_outputs_for_node(
        configuration: AgentTeamConfiguration,
        state: AgentTeamRunState,
        node_id: str,
    ) -> list[dict[str, Any]]:
        """只提取当前节点直接前置节点最近一次提交的结果。"""

        incoming_sources = [
            transition.from_node_id
            for transition in configuration.transitions
            if transition.target_node_id == node_id
        ]
        source_ids = list(dict.fromkeys(incoming_sources))
        if not source_ids:
            return []
        return state.previous_outputs_for(source_ids)

    @staticmethod
    def _workspace_root(workspace_id: int) -> str:
        """读取 workspace 根路径。"""

        return service_depends.get_workspace_service().get_workspace(workspace_id).root_path

    def _settle_node_result(
        self,
        node_run_id: int,
        status: str,
        output: str,
    ) -> tuple[Any, Any] | None:
        """结算节点结构化结果并创建下一节点；返回下一节点的 Task 与 Run。"""

        if len(output) > 100_000:
            raise ValueError("node output must be no longer than 100000 characters")
        row = self._team_run_crud.find_by_node_run_id(node_run_id)
        if row is None:
            raise ValueError("ConversationRun 不属于 Agent Team 节点")
        with self._lock_for(row.id):
            row = self._team_run_crud.get_by_id(row.id)
            if row.status != AgentTeamRunStatus.RUNNING.value:
                return None
            configuration = self._configurations.get(row.id)
            if configuration is None:
                configuration = AgentTeamConfiguration.model_validate(
                    row.configuration_snapshot_json
                )
                self._configurations[row.id] = configuration
            state = AgentTeamRunState.model_validate(row.state_json)
            execution = state.execution_for_run(node_run_id)
            if execution is None or state.active_node_run_id != node_run_id:
                raise ValueError("节点 Run 不是 Agent Team 当前活动节点")
            if execution.completed:
                return None

            node_id = execution.node_id
            node = configuration.node(node_id)
            if status not in node.statuses:
                raise ValueError(f"status '{status}' is not allowed by node '{node_id}'")

            state.complete_node(node_run_id, status, output)
            transitions = configuration.transitions_for(node_id, status)
            target_node_id = transitions[0].target_node_id if transitions else None
            state.add_transition(node_id, status, target_node_id)
            if node.node_type == "end":
                self._team_run_crud.update_status_if_in(
                    row.id,
                    AgentTeamRunStatus.COMPLETED.value,
                    (AgentTeamRunStatus.RUNNING.value,),
                    state=state,
                    ended=True,
                )
                return None
            if not transitions:
                self._team_run_crud.update_status_if_in(
                    row.id,
                    AgentTeamRunStatus.FAILED.value,
                    (AgentTeamRunStatus.RUNNING.value,),
                    state=state,
                    end_reason=AgentTeamRunEndReason.TRANSITION_NOT_FOUND.value,
                    ended=True,
                )
                return None
            if target_node_id is None:
                raise RuntimeError("有效转移缺少目标节点")

            try:
                with begin_immediate(self._session_factory) as session:
                    updated = self._team_run_crud.update_status_if_in(
                        row.id,
                        AgentTeamRunStatus.RUNNING.value,
                        (AgentTeamRunStatus.RUNNING.value,),
                        state=state,
                        session=session,
                    )
                    if updated is None:
                        return None
                    return self._start_node(
                        row.id,
                        target_node_id,
                        configuration=configuration,
                        session=session,
                    )
            except Exception:
                log.exception(
                    "agent_team_transition_start_failed",
                    extra={
                        "msg": "Agent Team 下一节点创建失败，已收敛 Team",
                        "data": {"run_id": row.id, "node_id": target_node_id},
                    },
                )
                self._fail_team(
                    row.id,
                    AgentTeamRunEndReason.NEXT_NODE_START_FAILED,
                    state=state,
                )
                return None

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
        if current.status != "running":
            return
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
        with self._lock_for(row.id):
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

    def shutdown(self) -> int:
        """关闭后端前收敛活动 Team 并取消 Coordinator 驱动任务。"""

        recovered = self.recover_after_restart()
        with self._team_drivers_guard:
            drivers = list(self._team_drivers.values())
            self._team_drivers.clear()
        for driver in drivers:
            driver.cancel()
        with self._runtime_loops_guard:
            self._runtime_loops.clear()
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
