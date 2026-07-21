"""Coordinate task lifecycle and workflow execution."""

import logging
import os
from collections.abc import AsyncIterator

from langchain_core.language_models import BaseChatModel

from app.config.logging import (
    shutdown_logging,
    trace_log_extra,
)
from app.config.settings import BackendSettings
from app.core.agents.profile import AgentProfile, default_developer_agent
from app.core.context import TextContextBuilder
from app.core.runtime.runs.checkpointer import build_checkpointer
from app.core.runtime.runtime_operations import RuntimeOperations
from app.core.workflows.agent_workflow import AgentWorkflow
from app.core.workflows.react.react_like import ReactLikeWorkflow
from app.core.events.types import EventType, RuntimeEvent
from app.service.log_query_service import LogQueryService
from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService
from app.models import TaskRecord
from app.models import TurnRecord
from app.storage.crud.durable_crud import DurableRunStore
from app.tools.tool_execute.tool_scheduler import ToolScheduler


class AgentRuntime:
    """Execute tasks and stream runtime events.

    单一职责：作为执行 / 生命周期引擎，负责任务状态推进、模型流消费、工具调度、
    运行时事件记录、取消与终止保护，以及从 LangGraph checkpoint 派生事件。

    职责边界：
    - 负责：任务执行编排、运行时事件、checkpoint 回放、取消。
    - 不负责：工作区 / 任务 / 轮次的 CRUD 与查询（委托给对应 service 层）；
      不对外暴露 service 访问器，service 仅作为本引擎的私有协作者。
    """

    def __init__(
        self,
        settings: BackendSettings,
        task_service: TaskService,
        turn_service: TurnService,
        context_builder: TextContextBuilder,
        tool_scheduler: ToolScheduler,
        logger: logging.Logger,
        agent_profile: AgentProfile | None = None,
        workflow: AgentWorkflow | None = None,
        run_store: DurableRunStore | None = None,
        log_query_service: LogQueryService | None = None,
    ) -> None:
        """Initialize the execution engine with its private collaborators.

        参数:
            settings: 后端运行配置。
            task_service: 任务编排服务（私有协作者，不对外暴露）。
            turn_service: 轮次编排服务（私有协作者，不对外暴露）。
            context_builder: 文本上下文构建器。
            tool_scheduler: 工具调度器。
            logger: 运行时日志器。
            agent_profile: 可选 Agent 角色配置。
            workflow: 可选执行策略。
            run_store: 可选持久化运行存储。
            log_query_service: 可选日志查询服务。
        """

        self._settings = settings
        self._task_service = task_service
        self._turn_service = turn_service
        self._context_builder = context_builder
        self._tool_scheduler = tool_scheduler
        self._logger = logger
        self._agent_profile = agent_profile or default_developer_agent()
        self._workflow = workflow or ReactLikeWorkflow()
        self._run_store = run_store

    def close(self) -> None:
        """Close external resources held by the runtime."""
        close_errors: list[Exception] = []
        for store in (self._run_store,):
            close = getattr(store, "close", None)
            if close is not None:
                try:
                    close()
                except Exception as exc:
                    close_errors.append(exc)
        for exc in close_errors:
            self._logger.warning(
                "runtime_resource_close_failed",
                extra={"msg": "runtime resource close failed", "data": {"error": str(exc)}},
            )
        shutdown_logging(self._logger.name)

    def cancel_task(self, task_id: str) -> TaskRecord:
        """Cancel a task and mark associated runs as cancelled."""

        runs = self._run_store.list_by_task(task_id) if self._run_store is not None else []
        try:
            task = self._task_service.update_status(task_id, "cancelled")
            for item in runs:
                self._mark_run_for_turn(
                    item.turn_id, "cancelled", interruption_reason="task_cancelled"
                )
            self._record(EventType.RUN_CANCELLED, task_id, {"status": "cancelled"})
            self._logger.info(
                "task_cancelled",
                extra={
                    "msg": "task cancelled",
                    "data": {"task_id": task_id},
                },
            )
            return task
        finally:
            pass

    async def run_task(self, task_id: str) -> AsyncIterator[RuntimeEvent]:
        """Run the latest turn for a task."""

        turn = self._turn_service.get_turn_for_task(task_id)
        async for event in self.run_turn(turn.turn_id):
            yield event

    async def run_turn(
        self,
        turn_id: str,
        turn: TurnRecord | None = None,
        model: BaseChatModel | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """Run or replay a specific turn.

        参数:
            turn_id: 需要运行的轮次标识符。
            turn: 可选的预取轮次记录；缺省时按 ``turn_id`` 读取。
            model: 可选注入的 LangChain chat model；缺省时由工作流按配置构建。
        """

        if turn is None:
            turn = self._turn_service.get_turn(turn_id)
        # 测试注入模型优先；否则由工作流按配置构建（echo/无 Key 时为离线模型）。
        model = model or getattr(self, "_default_test_model", None)
        task_id = turn.task_id
        task = self._task_service.get_task(task_id)
        if task.status == "cancelled":
            if turn.status != "cancelled":
                self._turn_service.update_turn_status(turn.turn_id, "cancelled")
            yield self._record(
                EventType.RUN_CANCELLED,
                task.task_id,
                {"status": "cancelled", "_turn_id": turn.turn_id},
            )
            return
        if turn.status == "running":
            async for event in self._replay_events_from_checkpoint(turn.turn_id, task.task_id):
                yield event
            return
        if turn.status != "pending":
            async for event in self._replay_events_from_checkpoint(turn.turn_id, task.task_id):
                yield event
            return

        agent_profile = self._resolve_task_agent_profile(task)
        if agent_profile is None:
            self._task_service.update_status(task.task_id, "failed")
            self._turn_service.update_turn_status(turn.turn_id, "failed")
            self._mark_run_for_turn(
                turn.turn_id,
                "failed",
                interruption_reason="agent_profile_unavailable",
            )
            self._logger.error(
                "agent_profile_unavailable",
                extra=trace_log_extra(
                    self._trace_context_for_task(task.task_id),
                    msg="agent profile unavailable for task",
                    data={
                        "task_id": task.task_id,
                        "task_agent_id": task.agent_id,
                        "runtime_agent_id": self._agent_profile.agent_id,
                    },
                ),
            )
            yield self._record(
                EventType.RUN_FAILED,
                task.task_id,
                {
                    "status": "failed",
                    "error": "agent_profile_unavailable",
                    "task_agent_id": task.agent_id,
                    "runtime_agent_id": self._agent_profile.agent_id,
                    "_turn_id": turn.turn_id,
                },
            )
            return

        if not self._turn_service.claim_pending_turn(turn.turn_id):
            async for event in self._stream_running_turn_events(turn.turn_id):
                yield event
            return
        self._task_service.update_status(task.task_id, "running")
        if self._run_store is not None:
            self._run_store.create_for_turn(task.task_id, turn.turn_id, "running")
        self._mark_run_for_turn(turn.turn_id, "running")
        yield self._record(
            EventType.RUN_STARTED,
            task.task_id,
            {"status": "running", "agent": agent_profile.to_dict(), "_turn_id": turn.turn_id},
        )

        operations = RuntimeOperations(
            settings=self._settings,
            task_store=self._task_store,
            context_builder=self._context_builder,
            tool_scheduler=self._tool_scheduler,
            logger=self._logger,
            agent_profile=agent_profile,
            current_turn_id=turn.turn_id,
        )

        try:
            async for event in self._workflow.run(task, operations, model=model):
                yield event
            final_task = self._task_service.get_task(task.task_id)
            if final_task.status in {"completed", "failed", "cancelled"}:
                self._turn_service.update_turn_status(turn.turn_id, final_task.status)
            self._sync_run_with_turn_status(turn.turn_id, final_task)
            return
        except Exception as exc:
            self._task_service.update_status(task.task_id, "failed")
            self._turn_service.update_turn_status(turn.turn_id, "failed")
            self._mark_run_for_turn(turn.turn_id, "failed", interruption_reason=str(exc))
            self._logger.exception(
                "task_failed",
                extra={
                    "msg": "task execution failed",
                    "data": {"task_id": task.task_id},
                },
            )
            event = RuntimeEvent(
                event_type=EventType.RUN_FAILED,
                task_id=task.task_id,
                turn_id=turn.turn_id,
                payload={"status": "failed", "error": str(exc)},
            )
            yield event

    async def _stream_running_turn_events(self, turn_id: str) -> AsyncIterator[RuntimeEvent]:
        """从 checkpoint 重放一个正在运行轮次的事件（尽力而为）。"""

        turn = self._turn_service.get_turn(turn_id)
        async for event in self._replay_events_from_checkpoint(turn_id, turn.task_id):
            yield event

    async def _replay_events_from_checkpoint(
        self, turn_id: str, task_id: str
    ) -> AsyncIterator[RuntimeEvent]:
        """从 LangGraph checkpoint 派生一个轮次的事件（替代自研 EventModel 重放）。

        自研运行时事件持久化已移除，事件由 LangGraph checkpoint 承载。这里读取该轮次
        （thread_id = turn_id）的最终 graph state，合成为前端可用的业务事件。仅覆盖最新
        一轮，且为尽力而为的近似重放。

        参数:
            turn_id: 需要重放的轮次标识符（同时作为 checkpoint thread_id）。
            task_id: 事件关联的任务标识符。

        生成:
            由 checkpoint 最终状态合成的业务事件。
        """

        try:
            async with build_checkpointer() as checkpointer:
                state = await checkpointer.aget_state({"configurable": {"thread_id": turn_id}})
        except Exception:
            self._logger.exception(
                "checkpoint_replay_failed",
                extra={
                    "msg": "failed to read checkpoint for event replay",
                    "data": {"task_id": task_id, "turn_id": turn_id},
                },
            )
            return
        if state is None or not state.values:
            return
        values = state.values
        final_text = values.get("final_text") or ""
        if values.get("final_response") and final_text:
            yield RuntimeEvent(
                event_type=EventType.FINAL_RESPONSE,
                task_id=task_id,
                turn_id=turn_id,
                payload={"text": final_text},
            )
            yield RuntimeEvent(
                event_type=EventType.RUN_FINISHED,
                task_id=task_id,
                turn_id=turn_id,
                payload={"status": "completed"},
            )
        elif values.get("terminal"):
            yield RuntimeEvent(
                event_type=EventType.RUN_FAILED,
                task_id=task_id,
                turn_id=turn_id,
                payload={"status": "failed", "error": "task_terminal_without_final_response"},
            )
        else:
            yield RuntimeEvent(
                event_type=EventType.RUN_FAILED,
                task_id=task_id,
                turn_id=turn_id,
                payload={"status": "incomplete", "error": "no_final_response"},
            )

    def _sync_run_with_turn_status(self, turn_id: str, task: TaskRecord) -> None:
        """Synchronize durable run status from final task state."""

        if task.status in {"completed", "failed", "cancelled"}:
            self._mark_run_for_turn(turn_id, task.status)

    def _mark_run_for_turn(
        self,
        turn_id: str,
        status: str,
        wait_reason: str | None = None,
        active_step_id: str | None = None,
        active_wait_id: str | None = None,
        interruption_reason: str | None = None,
    ) -> None:
        """Synchronize durable run status for a turn."""

        if self._run_store is None:
            return
        try:
            run = self._run_store.get_by_turn(turn_id)
            if run is None:
                self._logger.warning(
                    "durable_run_missing",
                    extra={
                        "msg": "durable run is missing for turn",
                        "data": {"turn_id": turn_id, "target_status": status},
                    },
                )
                return
            self._run_store.mark_status(
                run.run_id,
                status,
                wait_reason=wait_reason,
                active_step_id=active_step_id,
                active_wait_id=active_wait_id,
                interruption_reason=interruption_reason,
            )
        except Exception:
            self._logger.exception(
                "durable_run_status_sync_failed",
                extra={
                    "msg": "durable run status sync failed",
                    "data": {
                        "turn_id": turn_id,
                        "target_status": status,
                        "interruption_reason": interruption_reason,
                    },
                },
            )

    async def list_events(self, task_id: str) -> list:
        """从 LangGraph checkpoint 派生任务的运行时事件（替代自研 EventModel 查询）。

        参数:
            task_id: 任务标识符。

        返回:
            由 checkpoint 最终状态合成的业务事件列表。

        异常:
            KeyError: 如果任务不存在（由 API 层转换为 404）。
        """

        self._task_service.get_task(task_id)
        turn = self._turn_service.get_turn_for_task(task_id)
        events = []
        async for event in self._replay_events_from_checkpoint(turn.turn_id, task_id):
            events.append(event)
        return events

    def backend_health(self) -> dict:
        """Return backend model configuration and availability summary."""

        return {
            "status": "ok",
            "model_provider": self._settings.model_provider,
            "model_base_url": self._settings.model_base_url,
            "model_name": self._settings.model_name,
            "model_thinking_mode": self._settings.model_thinking_mode,
            "model_api_key_env": self._settings.model_api_key_env,
            "has_model_api_key": bool(os.environ.get(self._settings.model_api_key_env)),
        }

    def _resolve_task_agent_profile(self, task: TaskRecord) -> AgentProfile | None:
        """Resolve the agent profile allowed to execute a task."""

        if task.agent_id == self._agent_profile.agent_id:
            return self._agent_profile
        return None

    def _record(self, event_type: EventType, task_id: str, payload: dict) -> RuntimeEvent:
        """创建一条运行时事件。

        参数:
            event_type: 稳定的、机器可读的事件类型。
            task_id: 事件关联的任务标识符。
            payload: 可序列化为 JSON 的事件载荷（可含 ``_turn_id`` 键，会被提取为 turn_id）。

        返回:
            构造好的 RuntimeEvent。
        """

        payload = dict(payload)
        turn_id = payload.pop("_turn_id", None)
        tool_call_id = payload.get("tool_call_id")
        event = RuntimeEvent(
            event_type=event_type,
            task_id=task_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
            payload=payload,
        )
        self._logger.info(
            "runtime_event",
            extra={
                "msg": "runtime event recorded",
                "data": {
                    "task_id": task_id,
                    "event_type": str(event_type),
                    "event_id": event.event_id,
                },
            },
        )
        return event
