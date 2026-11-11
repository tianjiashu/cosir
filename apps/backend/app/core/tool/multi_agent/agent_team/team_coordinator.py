"""Agent Team 节点调度、结果提交和终态收敛。"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.tool.background.background_job import BackgroundConclusion
from app.core.tool.background.background_registry import DropReason, background_registry
from app.core.tool.background.cancel_placeholders import USER_CANCELLED_HINT
from app.core.tool.multi_agent.agent_message import prepare_agent_message
from app.core.tool.multi_agent.agent_team.agent_team_result import team_result_payload
from app.core.tool.multi_agent.agent_team.agent_team_run_archive_service import (
    AgentTeamRunArchiveService,
)
from app.core.tool.multi_agent.derivation import child_depth
from app.core.workflow.message_queue.task_message import AgentMessageBody
from app.models import ConversationRunRecord
from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.models.agent_team.configuration.team_node_definition import TeamNodeDefinition
from app.models.agent_team.state.agent_team_run_components import (
    AgentTeamNodeExecution,
    AgentTeamRunRuntime,
)
from app.models.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.models.conversation_input import MessageSource
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.models.message_metadata import BackgroundWork
from app.models.task_extra import TaskExtra, TaskNodeConfig
from app.models.task_kind import TaskType
from app.service.depends import (
    get_conversation_run_ledger,
    get_task_message_queue,
    get_task_service,
)
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

_ACTIVE_STATUSES = (AgentTeamRunStatus.RUNNING.value, AgentTeamRunStatus.PENDING.value)

_FINAL_ROUND_REMINDER = (
    "Team budget reminder: This is the Agent Team's final node execution ({remaining}/{total} "
    "remaining, including the current node). Complete the highest-value work this node can "
    "contribute. If the overall goal is met and this node has an allowed route that ends the "
    "Team, choose that route. Otherwise, use the allowed route that best advances the goal, make "
    "the final handoff, and clearly state any unfinished work. No execution budget will remain "
    "after this run."
)
# 按顺序匹配：阈值在小额度下可能重合（都取到 1 轮之类），先命中的生效。
_BUDGET_REMINDERS = (
    (
        0.1,
        "Team budget reminder: The shared node-execution budget for the entire Agent Team is at "
        "or below 10% ({remaining}/{total} remaining, including the current node). Focus on the "
        "highest-priority outcome for the overall goal. Avoid expanding scope or repeating "
        "cycles; if handing off, state the conclusions and remaining work clearly.",
    ),
    (
        0.3,
        "Team budget reminder: The shared node-execution budget for the entire Agent Team is at "
        "or below 30% ({remaining}/{total} remaining, including the current node). Narrow the "
        "scope and prioritize work essential to the overall goal. Avoid repeated exploration; for "
        "handoffs, clearly state conclusions, next steps, and blockers.",
    ),
    (
        0.5,
        "Team budget reminder: At most half of the shared node-execution budget for the entire "
        "Agent Team remains ({remaining}/{total}, including the current node). Prioritize the "
        "critical path toward the overall goal, avoid redundant work and unnecessary cycles, and "
        "preserve conclusions needed by downstream nodes when handing off.",
    ),
)


def _budget_reminder(remaining_rounds: int, max_rounds: int) -> str | None:
    """按 Team 共享的剩余节点执行预算（含当前这次）给出收敛提醒，未到阈值时为 None。

    提醒只在剩余预算恰好到达 50%、30%、10% 阈值时发一次；离散额度取第一个不高于阈值的整数轮数。
    最后一次执行机会单独说明，避免把小额度下的最后一轮误报为约一成。
    """
    if remaining_rounds == 1:
        return _FINAL_ROUND_REMINDER.format(remaining=remaining_rounds, total=max_rounds)
    for ratio, template in _BUDGET_REMINDERS:
        if remaining_rounds == max(1, int(max_rounds * ratio)):
            return template.format(remaining=remaining_rounds, total=max_rounds)
    return None


@dataclass
class _TeamExecution:
    """仅由后端事件循环操作的执行句柄。待提交的启动、停止请求与节点等待共用一个生命周期。"""

    parent_task_id: int
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop_reason: AgentTeamRunEndReason | None = None
    changed: asyncio.Future[None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    node_task_id: int | None = None

    def request_stop(self, reason: AgentTeamRunEndReason, *, withdrawn: bool) -> None:
        self.stop_reason = self.stop_reason or reason
        # 唤醒在等节点变化的执行，包括先单停、随后又关闭后端的情况。
        self.changed.set_result(None)
        self.changed = asyncio.get_running_loop().create_future()
        # 已撤销的调用无需等回执。单独停止仍要先持久化回执，才能回投取消档案。
        if withdrawn:
            self.ready.set()


class AgentTeamCoordinator:
    """持有已批准 Team 的执行任务，按节点串行推进；TeamRun 保存执行状态，登记表负责交付。"""

    def __init__(self) -> None:

        from app.config.configuration import get_tool_registry

        self._team_run_crud = AgentTeamRunCrud()
        self._task_service = get_task_service()
        self._tool_registry = get_tool_registry()
        self._ledger = get_conversation_run_ledger()
        self._run_archive_service = AgentTeamRunArchiveService()

        self._active: dict[tuple[int, str], tuple[_TeamExecution, asyncio.Task[None]]] = {}
        self._closing = False

    async def _stage(self, row: AgentTeamRunModel, run_id: int) -> BackgroundWork:
        """在后端事件循环登记已批准的作业，回执提交后才允许它调度节点。"""
        if self._closing:
            raise RuntimeError("backend is shutting down")
        execution = _TeamExecution(row.parent_task_id)
        loop = asyncio.get_running_loop()

        def cancel(reason: DropReason) -> None:
            end_reason = {
                "user": AgentTeamRunEndReason.CANCELLED,
                "withdraw": AgentTeamRunEndReason.CANCELLED,
                "parent": AgentTeamRunEndReason.PARENT_CANCELLED,
                "shutdown": AgentTeamRunEndReason.RUNTIME_RESTARTED,
            }[reason]
            loop.call_soon_threadsafe(
                lambda: execution.request_stop(end_reason, withdrawn=reason != "user")
            )

        work = background_registry.register(
            row.tool_call_id, task_id=row.parent_task_id, run_id=run_id, kind="agent_team",
            label=row.configuration_snapshot_json["name"], on_cancel=cancel,
            after_commit=execution.ready.set, reports_user_stop=True,
        )
        task = loop.create_task(self._execute(row, execution), name=f"agent-team-{row.id}")
        self._active[row.parent_task_id, row.tool_call_id] = execution, task
        return work

    def prepare_background(
        self, row: AgentTeamRunModel, run_id: int, loop: asyncio.AbstractEventLoop
    ) -> BackgroundWork:
        """工具线程只等事件循环接手作业。登记失败就收敛已确认的 TeamRun。"""
        try:
            return asyncio.run_coroutine_threadsafe(self._stage(row, run_id), loop).result()
        except Exception:
            self._team_run_crud.update_status_if_in(
                row.id, AgentTeamRunStatus.FAILED.value, (AgentTeamRunStatus.RUNNING.value,),
                end_reason=AgentTeamRunEndReason.TEAM_START_FAILED.value, ended=True,
            )
            self._run_archive_service.write_terminal_archive(row.id)
            raise

    async def _execute(self, row: AgentTeamRunModel, execution: _TeamExecution) -> None:
        """完成节点收尾及档案写入后，尝试一次交付；被撤销的登记自动丢弃迟到结论。"""
        try:
            await execution.ready.wait()
            final = await self._drive(row, execution)
            payload = await asyncio.to_thread(team_result_payload, final)
            user_stopped = final.end_reason == AgentTeamRunEndReason.CANCELLED.value
            if user_stopped:
                payload["hint"] = (
                    "Review completed nodes in the archive. "
                    "Do not resubmit without asking the user."
                )
            conclusion = BackgroundConclusion(
                payload, cancel_hint=USER_CANCELLED_HINT if user_stopped else None,
            )
        except Exception as exc:
            log.exception("agent_team_background_failed", extra={
                "msg": "Team 后台执行收尾失败",
                "data": {"team_run_id": row.id, "tool_call_id": row.tool_call_id},
            })
            conclusion = BackgroundConclusion({
                "team_run_id": row.id, "status": "failed", "error": str(exc),
            })
        try:
            await asyncio.to_thread(
                background_registry.complete, row.parent_task_id, row.tool_call_id, conclusion
            )
        finally:
            self._active.pop((row.parent_task_id, row.tool_call_id), None)

    async def _drive(
        self, team_run: AgentTeamRunModel, execution: _TeamExecution,
    ) -> AgentTeamRunModel:
        """在循环上等待节点，在工作线程里做短时存储操作，直到全部节点与档案落定。"""
        state: AgentTeamRunState | None = None
        try:
            state = AgentTeamRunState.model_validate(team_run.state_json)
            configuration = AgentTeamConfiguration.model_validate(
                team_run.configuration_snapshot_json
            )
            state.current_node_id = configuration.start_node_id
            next_node_input = None
            rounds = 0

            while execution.stop_reason is None and await asyncio.to_thread(
                self._persist_running, team_run.id, state
            ):
                if rounds >= configuration.max_runs:
                    await asyncio.to_thread(
                        self._fail,
                        team_run.id, state, AgentTeamRunEndReason.MAX_RUNS_EXCEEDED,
                        msg="Agent Team 超过最大轮数，已收敛为失败",
                        data={"max_runs": configuration.max_runs},
                    )
                    break
                rounds += 1
                node_run = await self._run_node(
                    team_run,
                    state,
                    configuration,
                    configuration.max_runs - rounds + 1,
                    execution,
                    next_node_input,
                )
                # 节点因 Team 取消而停下时不再按节点失败归因。
                if node_run is None or execution.stop_reason is not None:
                    break
                node_data = {"node_id": state.current_node_id, "node_run_id": node_run.id}
                if node_run.status != ConversationRunStatus.COMPLETED.value:
                    await asyncio.to_thread(
                        self._fail,
                        team_run.id, state,
                        AgentTeamRunEndReason.NODE_RUN_CANCELLED
                        if node_run.status == ConversationRunStatus.CANCELLED.value
                        else AgentTeamRunEndReason.NODE_RUN_FAILED,
                        msg="Agent Team 节点执行未完成",
                        data={**node_data, "node_status": node_run.status},
                    )
                    break
                parsed_output = self._parse_handoff(node_run.final_output)
                if parsed_output is None:
                    await asyncio.to_thread(
                        self._fail,
                        team_run.id, state, AgentTeamRunEndReason.NODE_OUTPUT_INVALID,
                        msg="节点未返回有效的 Team 状态与输出",
                        data=node_data,
                    )
                    break
                status, next_node_input = parsed_output
                next_node_id = state.has_next_node(status)
                if next_node_id is None:
                    await asyncio.to_thread(
                        self._fail,
                        team_run.id, state, AgentTeamRunEndReason.TRANSITION_NOT_FOUND,
                        msg="节点业务状态没有匹配的转移",
                        data={**node_data, "business_status": status},
                    )
                    break
                state.add_transition(state.current_node_id, status, next_node_id)
                if next_node_id == "END":
                    # 结论在节点 Run 与运行档案里；``end_reason`` 只承载失败/取消原因码。
                    await asyncio.to_thread(
                        self._team_run_crud.update_status_if_in,
                        team_run.id, AgentTeamRunStatus.COMPLETED.value,
                        (AgentTeamRunStatus.RUNNING.value,), state=state, ended=True,
                    )
                    log.info(
                        "agent_team_completed",
                        extra={
                            "msg": "Agent Team 已完成",
                            "data": {"team_run_id": team_run.id, **node_data},
                        },
                    )
                    break
                state.current_node_id = next_node_id
        except Exception as exc:
            failure_reason = (
                AgentTeamRunEndReason.NEXT_NODE_START_FAILED.value
                if state is not None and state.current_node_id is not None
                else AgentTeamRunEndReason.TEAM_START_FAILED.value
            )
            log.exception(
                "agent_team_dispatch_failed",
                extra={
                    "msg": "Agent Team 节点调度异常",
                    "data": {
                        "team_run_id": team_run.id,
                        "node_id": state.current_node_id if state is not None else None,
                        "failure_code": failure_reason,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            # 原始异常保留在结构化日志，持久化只写稳定原因码供 UI 安全分类。
            await asyncio.to_thread(
                self._team_run_crud.update_status_if_in,
                team_run.id, AgentTeamRunStatus.FAILED.value,
                (AgentTeamRunStatus.RUNNING.value,), state=state,
                end_reason=failure_reason, ended=True,
            )
            # 异常可能发生在输入已投递之后（等新 Run 超时、等静止失败）：不停下节点，它会在没有
            # 协调者的情况下继续执行。用内存快照定位——此时节点执行记录可能尚未落库。
            node_execution = (
                state.node_execution_for_node(state.current_node_id)
                if state is not None and state.current_node_id is not None
                else None
            )
            if node_execution is not None:
                actor = task_runtime_spaces.get_or_create(node_execution.task_id).actor
                await actor.cancel("parent")
        finally:
            # 所有正常终态分支与调度异常共用同一档案出口；写入失败由档案 service 记录，
            # 不覆盖已经收敛的 TeamRun 结果。
            cancelled = None
            if execution.stop_reason is not None:
                cancelled = await asyncio.to_thread(
                    self._cancel_if_active, team_run.id, execution.stop_reason
                )
            if cancelled is None:
                await asyncio.to_thread(
                    self._run_archive_service.write_terminal_archive, team_run.id
                )
        return await asyncio.to_thread(self._team_run_crud.get_by_id, team_run.id)

    def _persist_running(
        self, team_run_id: int, state: AgentTeamRunState, *, session: Session | None = None,
    ) -> bool:
        """把调度快照写回仍在运行的 TeamRun；条件更新落空即 Team 已停止，返回假。"""

        row = self._team_run_crud.update_status_if_in(
            team_run_id,
            AgentTeamRunStatus.RUNNING.value,
            (AgentTeamRunStatus.RUNNING.value,),
            state=state,
            session=session,
        )
        if row is not None:
            return True
        log.info(
            "agent_team_stopped",
            extra={
                "msg": "TeamRun 已不在运行，调度停止",
                "data": {"team_run_id": team_run_id, "node_id": state.current_node_id},
            },
        )
        return False

    def _fail(
            self,
            team_run_id: int,
            state: AgentTeamRunState,
            reason: AgentTeamRunEndReason,
            *,
            msg: str,
            data: dict[str, Any],
    ) -> None:
        """把运行中的 TeamRun 收敛为 failed，并以统一事件记录失败原因码。"""

        self._team_run_crud.update_status_if_in(
            team_run_id, AgentTeamRunStatus.FAILED.value,
            (AgentTeamRunStatus.RUNNING.value,), state=state,
            end_reason=reason.value, ended=True,
        )
        log.warning(
            "agent_team_failed",
            extra={
                "msg": msg,
                "data": {"team_run_id": team_run_id, "failure_code": reason.value, **data},
            },
        )

    @staticmethod
    def _parse_handoff(output: str | None) -> tuple[str, str] | None:
        """读取节点交接结果 ``{"status", "output"}``；格式错误由调用方记为节点输出失败。"""

        try:
            json_output = json.loads(output) if output is not None else None
        except json.JSONDecodeError:
            return None
        if not isinstance(json_output, dict):
            return None
        status = json_output.get("status")
        node_output = json_output.get("output")
        if isinstance(status, str) and status and isinstance(node_output, str):
            return status, node_output
        return None

    async def _run_node(
        self, row: AgentTeamRunModel, state: AgentTeamRunState,
        configuration: AgentTeamConfiguration,
        remaining_rounds: int, execution: _TeamExecution, node_input: str | None,
    ) -> ConversationRunRecord | None:
        dispatched = await asyncio.to_thread(
            self._enqueue_node, row, state, configuration, remaining_rounds, node_input
        )
        if dispatched is None:
            return None
        node, message_id = dispatched
        execution.node_task_id = node.task_id
        try:
            run_id = await self._await_node_run(row.id, state, node, execution)
            if run_id is None:
                return None
            return await asyncio.to_thread(self._ledger.get_run, run_id)
        finally:
            await asyncio.to_thread(self._withdraw_node_input, row.id, node.task_id, message_id)

    @staticmethod
    def _withdraw_node_input(team_run_id: int, task_id: int, message_id: int) -> None:
        """结束这次调度。节点若在并入输入前退出，撤回该行；已并入的历史由节点保留。"""
        with main_session_factory().begin() as session:
            removed = get_task_message_queue().withdraw(session, task_id, message_id)
        if removed is not None:
            log.info("agent_team_node_input_withdrawn", extra={
                "msg": "撤回节点未消费的调度输入",
                "data": {"team_run_id": team_run_id, "task_id": task_id, "message_id": message_id},
            })

    def _enqueue_node(
        self, row: AgentTeamRunModel, state: AgentTeamRunState,
        configuration: AgentTeamConfiguration,
        remaining_rounds: int, node_input: str | None,
    ) -> tuple[AgentTeamNodeExecution, int] | None:
        """复用或派生节点 Task，节点绑定与调度输入同事务提交；返回本次输入的队列身份。"""
        # _drive 已设置当前节点；确认阶段已解析并冻结它的模型配置。
        node_id = cast(str, state.current_node_id)
        node: TeamNodeDefinition = configuration.node(node_id)
        node_runtime: AgentTeamRunRuntime = state.node_runtime[node_id]
        node_execution: AgentTeamNodeExecution | None = state.node_execution_for_node(node_id)
        input_text = self._build_node_input(
            node_goal=node_runtime.node_goal,
            remaining_rounds=remaining_rounds,
            max_rounds=configuration.max_runs,
            node_input=node_input,
        )

        if node_execution is None:
            main_task = self._task_service.get_task(row.parent_task_id)
            task = self._task_service.create_task(
                workspace_id=row.workspace_id,
                title=node.name,
                task_type=TaskType.TEAM_NODE_TASK,
                parent_task_id=row.parent_task_id,
                extra=TaskExtra(team_node=TaskNodeConfig(
                    role=node_runtime.role,
                    system_prompt=node_runtime.system_prompt,
                    structured_output=node_runtime.structured_output,
                    model_settings=node_runtime.model_settings,
                )),
                tool_schemas=self._resolve_node_tool_schemas(node_runtime.allowed_tools),
                derivation_depth=child_depth(main_task, "team"),
            )
            node_execution = state.create_execution_node(
                node_id, AgentTeamNodeExecution(node_id=node_id, task_id=task.id)
            )

        # 节点输入是 Team 交给节点的话，以 Agent 消息投递：来源是发起 Team 的主 Task。
        message = prepare_agent_message(
            input_text,
            source=MessageSource(task_id=row.parent_task_id, name=configuration.name),
            agent_id=node.agent_id,
            model_config_id=cast(str, node_runtime.model_config_id),
            reasoning_effort=node_runtime.model_settings.get("reasoning_effort"),
        )
        with main_session_factory().begin() as session:
            if not self._persist_running(row.id, state, session=session):
                return None
            message_id = task_runtime_spaces.enqueue(
                node_execution.task_id, message, session=session,
            )
        return node_execution, message_id

    async def _await_node_run(
        self, team_run_id: int, state: AgentTeamRunState, node: AgentTeamNodeExecution,
        execution: _TeamExecution,
    ) -> int | None:
        """响应 actor 状态变化与 Team 停止请求。新 Run 出现后才取消，防止错过已入队的节点。"""
        actor = task_runtime_spaces.get_or_create(node.task_id).actor
        known = set(node.conversation_run_ids)
        node_run_id = None
        cancel_sent = False
        while True:
            changed = actor.state_changed
            control = execution.changed
            if actor.stopped.is_set():
                execution.request_stop(AgentTeamRunEndReason.CANCELLED, withdrawn=True)
                return None
            current = await asyncio.to_thread(self._task_service.require_current_run, node.task_id)
            if current is not None and current.id not in known:
                node_run_id = current.id
                known.add(node_run_id)
                cancel_sent = False
                node.conversation_run_ids.append(node_run_id)
                await asyncio.to_thread(self._persist_running, team_run_id, state)
            if self._closing and node_run_id is not None:
                # 关闭不能等模型再发一帧才停。先停已启动的节点，再由 _run_node 撤回尚未并入的输入。
                await actor.abort()
                await actor.cancel("parent")
                return node_run_id
            if execution.stop_reason is not None and node_run_id is not None and not cancel_sent:
                await actor.cancel("parent")
                cancel_sent = True
            if await actor.wait_quiescent(0):
                # Run 建立与静止判定可能跨过一次推进，须先读这次新状态。
                if changed.is_set():
                    continue
                if node_run_id is None and execution.stop_reason is None:
                    raise RuntimeError(f"task {node.task_id} 的节点输入没有启动新 Run")
                return node_run_id
            changed_wait = asyncio.create_task(changed.wait())
            try:
                await asyncio.wait((changed_wait, control), return_when=asyncio.FIRST_COMPLETED)
            finally:
                changed_wait.cancel()
                await asyncio.gather(changed_wait, return_exceptions=True)

    async def drain_for_deletion(self, task_ids: set[int]) -> None:
        """删除闸门外停止受影响的协调任务并等到收尾，之后才能删除节点和档案。"""
        waiting = []
        for (owner, call_id), (execution, task) in self._active.items():
            if execution.parent_task_id in task_ids:
                background_registry.withdraw(owner, call_id, reason="parent")
                execution.request_stop(AgentTeamRunEndReason.PARENT_CANCELLED, withdrawn=True)
                waiting.append(task)
            elif execution.node_task_id in task_ids:
                execution.request_stop(AgentTeamRunEndReason.CANCELLED, withdrawn=True)
                waiting.append(task)
        if waiting:
            await asyncio.gather(*waiting)

    async def aclose(self) -> None:
        """登记表已关闭，停止并等到全部执行退出，再关闭 actor 与服务依赖。"""
        self._closing = True
        for execution, _ in self._active.values():
            execution.request_stop(AgentTeamRunEndReason.RUNTIME_RESTARTED, withdrawn=True)
        await asyncio.gather(*(task for _, task in self._active.values()))

    def _resolve_node_tool_schemas(self, allowed_tools: list[str]) -> list[dict[str, object]]:
        """按节点快照声明的 ``allowed_tools`` 名字，从当前工具注册表解析冻结 schema。

        节点运行快照只持久化工具名，schema 只在首次执行该节点、创建节点 Task 时解析并固化进
        Task：快照不必携带易漂移的 schema 契约，再次访问节点沿用 Task 已固化的那份。

        异常:
            ValueError: 某个工具名不在注册表中（准备阶段对注册表里没有的名字原样保留，在这里如实
                报错，见 ``materialize_node_tools``）。
        """
        schemas: list[dict[str, object]] = []
        for name in allowed_tools:
            tool = self._tool_registry.get_tool_definition(name)
            if tool is None:
                raise ValueError(
                    f"Team 节点声明的工具 '{name}' 不在工具注册表中，无法解析其 schema"
                )
            schemas.append(copy.deepcopy(tool.to_model_tool_definition()))
        return schemas

    @staticmethod
    def _build_node_input(
            node_goal: str,
            remaining_rounds: int,
            max_rounds: int,
            node_input: str | None = None,
    ) -> str:
        """组装节点输入：子目标（确认边界保证非空）、上一节点交接，与按剩余预算的收敛提醒。"""

        sections = [f"Current node subgoal:\n{node_goal}"]
        if node_input:
            sections.append("Previous node output:\n" + node_input)
        reminder = _budget_reminder(remaining_rounds, max_rounds)
        if reminder is not None:
            sections.append(reminder)
        return "\n\n".join(sections)

    def _cancel_if_active(
            self,
            team_run_id: int,
            end_reason: AgentTeamRunEndReason,
    ) -> AgentTeamRunModel | None:
        """条件迁移为 cancelled 并写档案；TeamRun 已不在活动态（含并发落定）时返回 None。

        只迁移 TeamRun 并保存档案。属于后台执行的节点由执行任务自己停止并等待。
        """

        row = self._team_run_crud.update_status_if_in(
            team_run_id,
            AgentTeamRunStatus.CANCELLED.value,
            _ACTIVE_STATUSES,
            end_reason=end_reason.value,
            ended=True,
        )
        if row is None:
            return None
        self._run_archive_service.write_terminal_archive(row.id)
        log.info(
            "agent_team_run_cancelled",
            extra={
                "msg": "Agent Team 运行已取消",
                "data": {
                    "team_run_id": row.id,
                    "parent_run_id": row.parent_run_id,
                    "end_reason": end_reason.value,
                },
            },
        )
        return row

    def cancel_for_task_ids(self, task_ids: set[int]) -> int:
        """在删除任务树前取消与其关联的活动 Team。

        参数:
            task_ids: 即将删除的任务及其子任务标识集合。

        返回:
            实际从活动态收敛为 ``cancelled`` 的 Team 数量。

        异常:
            数据库异常：向上暴露，调用方应中止删除。

        副作用:
            取消以这些任务作为主 Agent 的 Team，以及节点 Task 位于集合中的 Team。节点 Task 在任务树
            里（``parent_task_id`` 是主 Task），由删除入口先经 ``task_runtime_spaces.stop`` 停下。
        """

        if not task_ids:
            return 0

        def involved(row: AgentTeamRunModel) -> bool:
            if row.parent_task_id in task_ids:
                return True
            state = AgentTeamRunState.model_validate(row.state_json)
            return any(node.task_id in task_ids for node in state.node_executions.values())

        return self._cancel_where(involved, AgentTeamRunEndReason.CANCELLED)

    def withdraw_pending_for_calls(self, parent_task_id: int, tool_call_ids: set[str]) -> int:
        """撤回回合时放弃这一回合提出、仍待确认的方案，返回收敛的数量。

        待确认的方案没有后台登记，撤回后的卡片已移出上下文、决定入口也找不到它，不在这里收敛就会
        一直停在 ``pending``。已批准运行中的 Team 经后台登记的撤销回调停下
        （``background_registry``），这里只碰 ``pending``。自建写事务，调用方须在自己的写事务之外调用。
        """
        if not tool_call_ids:
            return 0
        return self._cancel_where(
            lambda row: row.parent_task_id == parent_task_id
            and row.tool_call_id in tool_call_ids
            and row.status == AgentTeamRunStatus.PENDING.value,
            AgentTeamRunEndReason.CANCELLED,
        )

    async def cancel_for_parent_task(self, parent_task_id: int) -> None:
        """取消主 Agent Task 的全部活动 Team：待确认方案与运行中的 Team。

        由主 Task 被上游收回时调用（``TaskRecall``）；用户停止主 Task 不取消 Team。待批准的方案也随主 Task
        放弃。

        按 Task 而不按 Run 寻址：TeamRun 的 ``parent_run_id`` 是提出方案的 Run，批准可能发生在它
        结束之后，由新开的 Run 重放执行，按执行中的 Run 匹配会漏掉它。待确认方案一并取消：收回即
        放弃这个 Task 进行中的全部 Team 工作。

        已由事件循环接手的 Team 在节点停下后写终态。仍在确认或交接中的 Team 用同一次条件更新收敛，
        即使确认与停止同时发生也不会再开始节点。
        """
        tracked = set()
        for (_, call_id), (execution, _) in self._active.items():
            if execution.parent_task_id == parent_task_id:
                tracked.add(call_id)
                execution.request_stop(AgentTeamRunEndReason.PARENT_CANCELLED, withdrawn=True)
        await asyncio.to_thread(
            self._cancel_where,
            lambda row: row.parent_task_id == parent_task_id and row.tool_call_id not in tracked,
            AgentTeamRunEndReason.PARENT_CANCELLED,
        )

    def _cancel_where(
            self,
            matches: Callable[[AgentTeamRunModel], bool],
            end_reason: AgentTeamRunEndReason,
    ) -> int:
        """取消命中 ``matches`` 的待确认与运行中 TeamRun，返回实际收敛为 cancelled 的数量。"""

        cancelled = 0
        for row in (
                *self._team_run_crud.list_pending_confirmations(),
                *self._team_run_crud.list_active(),
        ):
            if matches(row) and self._cancel_if_active(row.id, end_reason) is not None:
                cancelled += 1
        return cancelled

    def recover_after_restart(self) -> list[int]:
        """把后端重启遗留的运行中 Team 收敛为 cancelled，不自动重放；返回这些 TeamRun 的节点
        Task id。

        待确认（pending）的 TeamRun 保留：它挂在仍处于等待态的主 Run 上，重启后用户照常批准、
        驳回或取消；收敛它会让仍可点击的审批卡片必然失败。节点 Task 交给 ``RecoverService``：挂起的
        节点 Run 再也没有 Coordinator 等它，要先取消，免得被重启结论或终端唤醒后独自继续。
        """

        node_task_ids: list[int] = []
        queue = get_task_message_queue()
        for row in self._team_run_crud.list_active():
            state = AgentTeamRunState.model_validate(row.state_json)
            nodes = [node.task_id for node in state.node_executions.values()]
            # 节点与主 Task 之间只有 Coordinator 能投递 Agent 消息。撤掉来不及消费的调度输入，
            # 否则启动期 actor 会为已经没有执行者的 Team 开新节点 Run。先撤回再迁移，可重复恢复。
            with main_session_factory().begin() as session:
                for node_id in nodes:
                    for message in queue.pending_nowait(node_id, session=session):
                        body = message.body
                        if (
                            isinstance(body, AgentMessageBody)
                            and body.source.task_id == row.parent_task_id
                        ):
                            queue.withdraw(session, node_id, cast(int, message.id))
            if self._cancel_if_active(row.id, AgentTeamRunEndReason.RUNTIME_RESTARTED) is not None:
                node_task_ids.extend(nodes)
        return node_task_ids
