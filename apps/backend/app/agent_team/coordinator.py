"""Agent Team 节点调度、结果提交和终态收敛。"""

from __future__ import annotations

import asyncio
import copy
import json
from collections import defaultdict
from concurrent.futures import Future
from threading import RLock
from typing import Any

from langchain_core.messages import SystemMessage
from sqlalchemy.orm import Session

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.assistant_transport.event import RunInitializedEvent, RunStatusChangedEvent
from app.assistant_transport.event.dispatch import dispatch_conversation_event
from app.config.logging.logger import log
from app.models.conversation_run_command import ConversationRunCommand
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service import depends as service_depends
from app.service.task.conversation_run_service import ConversationRunService
from app.service.task.conversation_run_state_service import ConversationRunStateService
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate
from app.task_runtime.service.task_service import TaskService


class AgentTeamCoordinator:
    """持有 TeamRun 进程内推进锁，并把节点委托给现有 Run 执行器。

    本类不拥有新的 worker 线程或队列；节点执行仍由现有
    ``ConversationRunExecutor`` 负责。锁仅保护同一 TeamRun 的状态提交和下一节点选择。
    """

    def __init__(self) -> None:
        self._team_run_crud = AgentTeamRunCrud()
        self._task_service = TaskService()
        self._run_service = ConversationRunService()
        self._run_state_service = ConversationRunStateService()
        self._session_factory = main_session_factory()
        self._locks_guard = RLock()
        self._locks: dict[int, RLock] = defaultdict(RLock)
        self._waiters_guard = RLock()
        self._waiters: dict[int, list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = (
            defaultdict(list)
        )
        self._runtime_loops_guard = RLock()
        self._runtime_loops: dict[int, asyncio.AbstractEventLoop] = {}
        self._parent_continuation_guard = RLock()
        self._parent_continuations: dict[int, Future[Any]] = {}

    def _lock_for(self, team_run_db_id: int) -> RLock:
        with self._locks_guard:
            return self._locks[team_run_db_id]

    def start(
        self,
        team_run_id: int,
        *,
        runtime_loop: asyncio.AbstractEventLoop,
    ) -> AgentTeamRunModel:
        """启动一条已确认的 TeamRun，并登记入口节点执行。

        本方法只接受已经持久化且完成确认的 ``running`` TeamRun，不读取待确认请求、预览
        文档或用户确认参数。TeamRun 的创建、确认和 ``pending`` 到 ``running`` 的原子迁移
        由执行请求 service 完成；本方法只创建入口节点并交给现有 ConversationRun 执行器。

        异常:
            KeyError: TeamRun 不存在。
            ValueError: TeamRun 不处于可启动的 ``running`` 状态。
            RuntimeError: 入口节点未能创建 ConversationRun。
        """

        initial = self._team_run_crud.get_by_id(team_run_id)
        if initial.status != "running":
            raise ValueError(f"TeamRun 当前状态不可启动: {initial.status}")
        row: AgentTeamRunModel | None = None
        try:
            with begin_immediate(self._session_factory) as session:
                row = session.get(AgentTeamRunModel, initial.id)
                if row is None:
                    raise KeyError(team_run_id)
                if row.status != "running":
                    raise ValueError(f"TeamRun 当前状态不可启动: {row.status}")
                configuration = AgentTeamConfiguration.model_validate(
                    row.configuration_snapshot_json
                )
                task, node_run = self._start_node(
                    row.id,
                    configuration.start_node_id,
                    runtime_loop,
                    session=session,
                )
                if task is None or node_run is None:
                    raise RuntimeError("入口节点未创建 ConversationRun")
            with self._runtime_loops_guard:
                self._runtime_loops[row.id] = runtime_loop
            continuation = asyncio.run_coroutine_threadsafe(
                self._resume_parent_after_team(row.id), runtime_loop
            )
            with self._parent_continuation_guard:
                self._parent_continuations[row.id] = continuation
            continuation.add_done_callback(
                lambda completed: self._forget_parent_continuation(row.id, completed)
            )
            self._publish_node_started(task, node_run)
            self._start_node_execution(node_run, runtime_loop)
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
        if row is None:
            raise RuntimeError("TeamRun creation did not return a row")
        return self._team_run_crud.get_by_id(row.id)

    async def _resume_parent_after_team(self, team_run_id: int) -> None:
        """Team 进入终态后，把受挂起的主 Agent Run 恢复并注入 TeamResult。"""

        try:
            team = await self.wait_until_terminal(team_run_id)
            from app.service.depends import (
                get_conversation_run_command_service,
                get_conversation_run_executor,
            )

            parent_run = await asyncio.to_thread(
                self._run_state_service.get_run, team.parent_run_id
            )
            forced_pause = False
            while parent_run.status not in {"cancelled", "completed", "failed"}:
                if parent_run.status == "cancelled":
                    break
                if parent_run.status in {"completed", "failed"}:
                    log.warning(
                        "agent_team_parent_run_not_resumable",
                        extra={
                            "msg": "Agent Team 已结束，但主 Agent Run 已进入不可恢复终态",
                            "data": {
                                "run_id": team_run_id,
                                "parent_run_id": team.parent_run_id,
                                "parent_status": parent_run.status,
                            },
                        },
                    )
                    return
                if not forced_pause:
                    # 正常路径已经由 observe 节点取消主 Run；如果 Team 先于该节点
                    # 收敛，主动发送同一取消语义，避免 TeamResult 因竞态永久丢失。
                    try:
                        await get_conversation_run_executor().cancel(
                            team.parent_run_id,
                            end_reason="agent_team_waiting_confirmation",
                        )
                    except Exception:
                        log.warning(
                            "agent_team_parent_executor_cancel_failed",
                            extra={
                                "msg": "主 Agent 执行器取消信号发送失败，继续用数据库状态收敛",
                                "data": {"run_id": team_run_id},
                            },
                        )
                    await asyncio.to_thread(
                        self._run_state_service.cancel_run_if_running,
                        team.parent_run_id,
                        "agent_team_waiting_confirmation",
                        "Agent Team 执行方案已生成，等待用户确认。",
                    )
                    forced_pause = True
                await asyncio.sleep(0.05)
                parent_run = await asyncio.to_thread(
                    self._run_state_service.get_run, team.parent_run_id
                )
            if parent_run.status in {"completed", "failed"}:
                log.warning(
                    "agent_team_parent_run_not_resumable",
                    extra={
                        "msg": "Agent Team 已结束，但主 Agent Run 已进入不可恢复终态",
                        "data": {
                            "run_id": team_run_id,
                            "parent_run_id": team.parent_run_id,
                            "parent_status": parent_run.status,
                        },
                    },
                )
                return
            if parent_run.end_reason != "agent_team_waiting_confirmation":
                log.warning(
                    "agent_team_parent_run_cancel_reason_mismatch",
                    extra={
                        "msg": "主 Agent Run 的取消原因不是 Agent Team 等待确认，跳过自动续跑",
                        "data": {
                            "run_id": team_run_id,
                            "parent_run_id": team.parent_run_id,
                            "end_reason": parent_run.end_reason,
                        },
                    },
                )
                return

            from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

            task_runtime_spaces.get_or_create(team.parent_task_id).defer_system_message(
                SystemMessage(
                    content=self._build_team_result_message(team),
                    additional_kwargs={"run_id": team.parent_run_id},
                )
            )
            attempt = 0
            while True:
                parent_run = await asyncio.to_thread(
                    self._run_state_service.get_run, team.parent_run_id
                )
                if parent_run.status in {"completed", "failed"}:
                    log.warning(
                        "agent_team_parent_run_not_resumable",
                        extra={
                            "msg": "主 Agent Run 在 TeamResult 续跑前已进入终态",
                            "data": {
                                "run_id": team_run_id,
                                "parent_run_id": team.parent_run_id,
                                "parent_status": parent_run.status,
                                "attempt": attempt,
                            },
                        },
                    )
                    return
                if parent_run.status != "cancelled":
                    if parent_run.status == "running":
                        try:
                            await get_conversation_run_executor().cancel(
                                team.parent_run_id,
                                end_reason="agent_team_waiting_confirmation",
                            )
                        except Exception:
                            log.exception(
                                "agent_team_parent_run_repause_failed",
                                extra={
                                    "msg": "主 Agent 续跑失败后重新暂停失败",
                                    "data": {"run_id": team_run_id},
                                },
                            )
                    await asyncio.sleep(0.2)
                    continue
                if parent_run.end_reason != "agent_team_waiting_confirmation":
                    log.warning(
                        "agent_team_parent_run_cancel_reason_mismatch",
                        extra={
                            "msg": "主 Agent Run 取消原因已变化，跳过自动续跑",
                            "data": {
                                "run_id": team_run_id,
                                "parent_run_id": team.parent_run_id,
                                "end_reason": parent_run.end_reason,
                            },
                        },
                    )
                    return
                try:
                    resumed = await asyncio.to_thread(
                        get_conversation_run_command_service().resume_latest_run,
                        team.parent_task_id,
                        team.parent_run_id,
                    )
                    await get_conversation_run_executor().start(
                        resumed.run.id, start_mode=resumed.execution_mode
                    )
                    log.info(
                        "agent_team_parent_run_resumed",
                        extra={
                            "msg": "Agent Team 已向主 Agent 注入 TeamResult 并恢复主 Run",
                            "data": {
                                "run_id": team_run_id,
                                "parent_task_id": team.parent_task_id,
                                "parent_run_id": team.parent_run_id,
                                "team_status": team.status,
                                "attempt": attempt,
                            },
                        },
                    )
                    return
                except Exception as exc:
                    attempt += 1
                    log.warning(
                        "agent_team_parent_run_resume_retry",
                        extra={
                            "msg": "主 Agent Run 续跑失败，将继续等待本地状态收敛后重试",
                            "data": {
                                "run_id": team_run_id,
                                "parent_run_id": team.parent_run_id,
                                "attempt": attempt,
                                "error_type": type(exc).__name__,
                            },
                        },
                    )
                    try:
                        await get_conversation_run_executor().cancel(
                            team.parent_run_id,
                            end_reason="agent_team_waiting_confirmation",
                        )
                    except Exception:
                        log.exception(
                            "agent_team_parent_resume_retry_cancel_failed",
                            extra={
                                "msg": "主 Agent 续跑失败后重新收敛取消状态失败",
                                "data": {"run_id": team_run_id},
                            },
                        )
                    await asyncio.sleep(min(5.0, 0.2 * attempt))
        except Exception:
            log.exception(
                "agent_team_parent_run_resume_failed",
                extra={
                    "msg": "Agent Team 结束后恢复主 Agent Run 失败",
                    "data": {"run_id": team_run_id},
                },
            )

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

    def _forget_parent_continuation(self, team_run_id: int, completed: Future[Any]) -> None:
        """移除已经完成的主 Agent 续跑任务，避免进程内注册表积累。"""

        with self._parent_continuation_guard:
            if self._parent_continuations.get(team_run_id) is completed:
                self._parent_continuations.pop(team_run_id, None)

    def _start_node(
        self,
        team_run_db_id: int,
        node_id: str,
        runtime_loop: asyncio.AbstractEventLoop,
        *,
        session: Session | None = None,
    ) -> tuple[Any | None, Any | None]:
        """创建一个节点 Task/Run，固化工具 schema 并调度现有执行器。"""

        row = (
            session.get(AgentTeamRunModel, team_run_db_id)
            if session is not None
            else self._team_run_crud.get_by_id(team_run_db_id)
        )
        if row is None:
            raise KeyError(team_run_db_id)
        configuration = AgentTeamConfiguration.model_validate(row.configuration_snapshot_json)
        state = AgentTeamRunState.model_validate(row.state_json)
        node = configuration.node(node_id)
        runtime_snapshot = state.runtime.node_snapshots.get(node_id)
        if not isinstance(runtime_snapshot, dict):
            raise ValueError(f"Team 节点缺少已冻结的运行快照: {node_id}")
        agent_id = runtime_snapshot.get("agent_id")
        tool_definitions = runtime_snapshot.get("tool_definitions")
        if not isinstance(agent_id, str) or not isinstance(tool_definitions, list):
            raise ValueError(f"Team 节点运行快照无效: {node_id}")
        if state.active_node_run_id is not None:
            raise ValueError("Team 已存在尚未完成的活动节点")
        previous_outputs = self._previous_outputs_for_node(configuration, state, node_id)
        input_text = self._build_node_input(
            row.goal_input,
            row.node_instructions_json.get(node_id, ""),
            previous_outputs,
        )
        parent_run = self._run_state_service.get_run(row.parent_run_id)
        model_settings = runtime_snapshot.get("model_settings", {})
        reasoning_effort = model_settings.get("reasoning_effort")

        def persist(persist_session: Session) -> tuple[Any, Any]:
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
                tool_definitions=copy.deepcopy(tool_definitions),
                session=persist_session,
            )
            node_run = self._run_service.create_run(
                task_id=task.id,
                agent_id=agent_id,
                status=ConversationRunStatus.RUNNING.value,
                model_config_id=runtime_snapshot.get("model_config_id")
                or parent_run.model_config_id,
                reasoning_effort=reasoning_effort,
                run_command=ConversationRunCommand(display_text=input_text),
                session=persist_session,
            )
            state.start_node(node_id, task.id, node_run.id)
            updated = self._team_run_crud.update_status_if_in(
                team_run_db_id,
                "running",
                ("running",),
                state=state,
                session=persist_session,
            )
            if updated is None:
                raise RuntimeError("TeamRun 已被其他路径处理，无法启动节点")
            return task, node_run

        if session is None:
            with begin_immediate(self._session_factory) as owned_session:
                task, node_run = persist(owned_session)
            self._publish_node_started(task, node_run)
            self._start_node_execution(node_run, runtime_loop)
        else:
            task, node_run = persist(session)
        return task, node_run

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

    def _start_node_execution(
        self,
        node_run: Any,
        runtime_loop: asyncio.AbstractEventLoop,
    ) -> None:
        """在节点持久化事实提交后启动现有 ConversationRunExecutor。"""

        from app.service.depends import get_conversation_run_executor

        if runtime_loop.is_closed():
            raise RuntimeError("runtime event loop is closed")
        future = asyncio.run_coroutine_threadsafe(
            get_conversation_run_executor().start(node_run.id, start_mode="fresh"),
            runtime_loop,
        )
        future.result(timeout=10)

    @staticmethod
    def _build_node_input(
        goal: str, instruction: str, previous_outputs: list[dict[str, Any]]
    ) -> str:
        """按固定三段协议拼出节点输入，不支持字段级映射。"""

        return "\n\n".join(
            (
                f"共同目标:\n{goal}",
                f"节点预设指令:\n{instruction}",
                "前置节点输出:\n" + json.dumps(previous_outputs, ensure_ascii=False),
            )
        )

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

    def submit_node_result(
        self,
        node_run_id: int,
        status: str,
        output: str,
        *,
        runtime_loop: asyncio.AbstractEventLoop | None,
    ) -> dict[str, Any]:
        """校验并提交节点结果，随后按唯一目标节点继续执行。"""

        if not isinstance(output, str) or len(output) > 100_000:
            raise ValueError("node output must be a string no longer than 100000 characters")
        row = self._team_run_crud.find_by_node_run_id(node_run_id)
        if row is None:
            raise ValueError("当前 ConversationRun 不属于活动 Agent Team 节点")
        with self._lock_for(row.id):
            row = self._team_run_crud.get_by_id(row.id)
            if row.status != "running":
                return {"status": "team_already_terminal", "run_id": row.id}
            configuration = AgentTeamConfiguration.model_validate(row.configuration_snapshot_json)
            state = AgentTeamRunState.model_validate(row.state_json)
            execution = state.execution_for_run(node_run_id)
            if execution is None:
                raise ValueError("当前节点 Run 不属于该 Agent Team")
            if execution.completed:
                return {"status": "node_already_completed", "run_id": row.id}
            if state.active_node_run_id != node_run_id:
                raise ValueError("节点状态提交者不是当前活动节点")
            node_id = execution.node_id
            node = configuration.node(node_id)
            if status not in node.statuses:
                raise ValueError(f"status '{status}' is not allowed by node '{node_id}'")
            self._run_state_service.complete_run_if_running(node_run_id, final_output=output)

            state.complete_node(node_run_id, status, output)
            transitions = configuration.transitions_for(node_id, status)
            target_node_id = None
            if transitions:
                target_node_id = transitions[0].target_node_id
            state.add_transition(node_id, status, target_node_id)
            if node.node_type == "end":
                updated = self._team_run_crud.update_status_if_in(
                    row.id,
                    "completed",
                    ("running",),
                    state=state,
                    ended=True,
                )
                if updated is None:
                    return {"status": "team_already_terminal", "run_id": row.id}
                self._notify_terminal(row.id)
                return {"status": "completed", "run_id": row.id}
            if not transitions:
                updated = self._team_run_crud.update_status_if_in(
                    row.id,
                    "failed",
                    ("running",),
                    state=state,
                    end_reason=AgentTeamRunEndReason.TRANSITION_NOT_FOUND.value,
                    ended=True,
                )
                if updated is None:
                    return {"status": "team_already_terminal", "run_id": row.id}
                self._notify_terminal(row.id)
                return {"status": "failed", "run_id": row.id}
            if target_node_id is None:
                raise RuntimeError("有效转移缺少目标节点")
            if runtime_loop is None or runtime_loop.is_closed():
                updated = self._team_run_crud.update_status_if_in(
                    row.id,
                    "failed",
                    ("running",),
                    state=state,
                    end_reason=AgentTeamRunEndReason.RUNTIME_UNAVAILABLE.value,
                    ended=True,
                )
                if updated is None:
                    return {"status": "team_already_terminal", "run_id": row.id}
                self._notify_terminal(row.id)
                return {"status": "failed", "run_id": row.id}

            pending_start: tuple[Any, Any] | None = None
            try:
                with begin_immediate(self._session_factory) as session:
                    updated = self._team_run_crud.update_status_if_in(
                        row.id,
                        "running",
                        ("running",),
                        state=state,
                        session=session,
                    )
                    if updated is None:
                        raise RuntimeError("TeamRun 已被其他路径处理，无法启动下一节点")
                    task, node_run = self._start_node(
                        row.id,
                        target_node_id,
                        runtime_loop,
                        session=session,
                    )
                    if task is None or node_run is None:
                        raise RuntimeError(f"节点 {target_node_id} 未创建 ConversationRun")
                    pending_start = (task, node_run)
            except Exception:
                log.exception(
                    "agent_team_transition_persist_failed",
                    extra={
                        "msg": "Agent Team 下一节点事务提交失败，已收敛 Team",
                        "data": {"run_id": row.id, "node_id": target_node_id},
                    },
                )
                self._fail_team(
                    row.id,
                    AgentTeamRunEndReason.NEXT_NODE_START_FAILED,
                )
                return {"status": "failed", "run_id": row.id}

            try:
                if pending_start is None:
                    raise RuntimeError("下一节点未创建 ConversationRun")
                task, node_run = pending_start
                self._publish_node_started(task, node_run)
                self._start_node_execution(node_run, runtime_loop)
            except Exception:
                log.exception(
                    "agent_team_transition_start_failed",
                    extra={
                        "msg": "Agent Team 下一节点启动失败，已收敛 Team",
                        "data": {"run_id": row.id, "node_id": target_node_id},
                    },
                )
                self._fail_team(
                    row.id,
                    AgentTeamRunEndReason.NEXT_NODE_START_FAILED,
                )
                return {"status": "failed", "run_id": row.id}
            return {
                "status": "running",
                "run_id": updated.id,
                "next_node_id": target_node_id,
            }

    def _fail_team(
        self,
        team_run_db_id: int,
        end_reason: AgentTeamRunEndReason,
    ) -> None:
        """把 Team 收敛为失败并保留受控原因。"""

        current = self._team_run_crud.get_by_id(team_run_db_id)
        if current.status != "running":
            return
        state = AgentTeamRunState.model_validate(current.state_json)
        completed_runs = state.completed_run_ids()
        row = self._team_run_crud.update_status_if_in(
            team_run_db_id,
            "failed",
            ("running",),
            end_reason=end_reason.value,
            ended=True,
        )
        if row is None:
            log.info(
                "agent_team_stale_failure_ignored",
                extra={
                    "msg": "忽略迟到的 Team 失败回调",
                    "data": {"team_run_db_id": team_run_db_id, "end_reason": end_reason.value},
                },
            )
            return
        self._notify_terminal(row.id)
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
            self._notify_terminal(result.id)
            state = AgentTeamRunState.model_validate(row.state_json)
            for _, node_run_id in state.node_references():
                self._cancel_node_execution(
                    node_run_id, row.id, end_reason="agent_team_cancelled"
                )
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
        for row in self._team_run_crud.list_active():
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
        传播到 Team 及其当前节点 Run。等待确认的内部暂停不会调用本方法。

        参数:
            parent_run_id: 主 Agent ConversationRun 标识。

        返回:
            实际从活动态收敛为 ``cancelled`` 的 Team 数量。

        副作用:
            更新 TeamRun 终态并取消节点执行器；异常向调用方暴露，供
            取消入口记录诊断日志，但不回滚主 Run 已发出的取消信号。
        """

        cancelled = 0
        for row in self._team_run_crud.list_active():
            if row.parent_run_id != parent_run_id:
                continue
            previous = row.status
            self.cancel(row.id)
            if previous in {"pending", "running"}:
                cancelled += 1
        return cancelled

    def handle_node_natural_completion(self, node_run_id: int) -> None:
        """节点自然结束但没有提交状态时，将 Team 明确收敛为失败。"""

        row = self._team_run_crud.find_by_node_run_id(node_run_id)
        if row is None:
            return
        with self._lock_for(row.id):
            current = self._team_run_crud.get_by_id(row.id)
            if current.status != "running":
                return
            state = AgentTeamRunState.model_validate(current.state_json)
            execution = state.execution_for_run(node_run_id)
            if execution is None:
                return
            if execution.completed:
                return
            node_run = self._run_state_service.get_run(node_run_id)
            if node_run.status == "failed":
                self._fail_team(
                    current.id,
                    AgentTeamRunEndReason.NODE_RUN_FAILED,
                )
                return
            if node_run.status == "cancelled":
                self._fail_team(
                    current.id,
                    AgentTeamRunEndReason.NODE_RUN_CANCELLED,
                )
                return
            self._fail_team(
                current.id,
                AgentTeamRunEndReason.IMPLICIT_COMPLETION_MISSING,
            )

    def recover_after_restart(self) -> int:
        """把后端重启遗留的活动 Team 收敛为 cancelled，不自动重放。"""

        active = self._team_run_crud.list_active()
        for row in active:
            updated = self._team_run_crud.update_status_if_in(
                row.id,
                "cancelled",
                ("running",),
                end_reason=AgentTeamRunEndReason.RUNTIME_RESTARTED.value,
                ended=True,
            )
            if updated is None:
                continue
            self._notify_terminal(updated.id)
        return len(active)

    def shutdown(self) -> int:
        """关闭后端前收敛活动 Team 并唤醒所有本地等待者。"""

        recovered = self.recover_after_restart()
        with self._parent_continuation_guard:
            continuations = list(self._parent_continuations.values())
            self._parent_continuations.clear()
        for continuation in continuations:
            continuation.cancel()
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

    async def wait_until_terminal(
        self, team_run_id: int, timeout_seconds: float | None = None
    ) -> AgentTeamRunModel:
        """异步等待 Team 终态，不阻塞事件循环或占用轮询线程。"""

        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        with self._waiters_guard:
            self._waiters[team_run_id].append((loop, event))
        try:
            current = self._team_run_crud.get_by_id(team_run_id)
            if current.status != "running":
                return current
            if timeout_seconds is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
            return self._team_run_crud.get_by_id(team_run_id)
        finally:
            with self._waiters_guard:
                waiters = self._waiters.get(team_run_id, [])
                self._waiters[team_run_id] = [item for item in waiters if item[1] is not event]
                if not self._waiters[team_run_id]:
                    self._waiters.pop(team_run_id, None)

    def _notify_terminal(self, team_run_id: int) -> None:
        """唤醒等待同一 TeamRun 终态的 asyncio 协程。"""

        with self._waiters_guard:
            waiters = list(self._waiters.pop(team_run_id, []))
        for loop, event in waiters:
            if not loop.is_closed():
                loop.call_soon_threadsafe(event.set)

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
