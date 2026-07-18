"""Coordinate task lifecycle and workflow execution."""

import asyncio
import logging
import os
from typing import AsyncIterator, Optional

from app.core.agents.profile import AgentProfile, default_developer_agent
from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.core.logs.query_service import LogQueryService
from app.core.trace.event_names import runtime_trace_event_name
from app.core.trace.query_service import TraceQueryService
from app.core.trace.recorder import TraceRecorder
from app.events.types import EventType, RuntimeEvent
from app.config.logging import bind_log_context, reset_log_context, set_log_context, shutdown_logging, trace_log_extra
from app.storage.crud.durable import DurableRunStore
from app.models.base import StreamingModelAdapter
from app.core.runtime.operations import RuntimeOperations
from app.storage.records import TaskRecord, TurnRecord, WorkspaceRecord
from app.tools.runtime.compatibility import ToolScheduler
from app.core.workflows.react_like import ReactLikeWorkflow
from app.core.workflows.types import AgentWorkflow



class AgentRuntime:
    """Coordinate task state, model streaming, runtime events, and logs."""

    def __init__(
        self,
        settings: BackendSettings,
        task_store,
        context_builder: TextContextBuilder,
        model_adapter: StreamingModelAdapter,
        tool_scheduler: ToolScheduler,
        logger: logging.Logger,
        agent_profile: Optional[AgentProfile] = None,
        workflow: Optional[AgentWorkflow] = None,
        run_store: Optional[DurableRunStore] = None,
        trace_recorder: Optional[TraceRecorder] = None,
        trace_query_service: Optional[TraceQueryService] = None,
        log_query_service: Optional[LogQueryService] = None,
    ) -> None:
        """Initialize runtime dependencies."""

        self._settings = settings
        self._task_store = task_store
        self._context_builder = context_builder
        self._model_adapter = model_adapter
        self._tool_scheduler = tool_scheduler
        self._logger = logger
        self._agent_profile = agent_profile or default_developer_agent()
        self._workflow = workflow or ReactLikeWorkflow()
        self._run_store = run_store
        self._trace_recorder = trace_recorder
        self._trace_query_service = trace_query_service
        self._log_query_service = log_query_service

    def close(self) -> None:
        """Close external resources held by the runtime."""
        close_errors: list[Exception] = []
        for store in (
            self._task_store,
            self._run_store,
            getattr(self._trace_recorder, "_store", None),
        ):
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

    def create_workspace(self, name: str, root_path: str) -> WorkspaceRecord:
        """Create a local workspace record."""

        workspace = self._task_store.create_workspace(name, root_path)
        self._logger.info(
            "workspace_created",
            extra={
                "msg": "workspace created",
                "data": {"workspace_id": workspace.workspace_id, "root_path": workspace.root_path},
            },
        )
        return workspace

    def list_workspaces(self) -> list[WorkspaceRecord]:
        """List local workspace records."""

        return self._task_store.list_workspaces()

    def delete_workspace(self, workspace_id: str) -> None:
        """Delete a workspace and related runtime records."""

        tasks = self._task_store.list_tasks_for_workspace(workspace_id)
        task_ids = [task.task_id for task in tasks]
        self._logger.info(
            "workspace_delete_start",
            extra={
                "msg": "workspace delete started",
                "data": {"workspace_id": workspace_id, "task_count": len(task_ids)},
            },
        )
        run_ids = []
        if self._run_store is not None:
            for task_id in task_ids:
                run_ids.extend(run.run_id for run in self._run_store.list_by_task(task_id))
        if self._run_store is not None:
            self._run_store.delete_by_task_ids(task_ids)
        if self._trace_recorder is not None:
            self._trace_recorder.delete_task_traces(task_ids)
        self._task_store.delete_workspace(workspace_id)
        self._logger.info(
            "workspace_deleted",
            extra={
                "msg": "workspace deleted",
                "data": {"workspace_id": workspace_id, "task_ids": task_ids, "run_ids": run_ids},
            },
        )

    def list_workspace_tasks(self, workspace_id: str) -> list[TaskRecord]:
        """List task records under a workspace."""

        return self._task_store.list_tasks_for_workspace(workspace_id)

    def create_task(
        self,
        input_text: str,
        workspace_id: Optional[str] = None,
    ) -> TaskRecord:
        """Create a pending task for later execution."""

        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        task = self._task_store.create_task(
            input_text=input_text,
            status="pending",
            agent_id=self._agent_profile.agent_id,
            workspace_id=workspace_id,
        )
        context = self._trace_context_for_task(task.task_id)
        token = set_log_context(context)
        try:
            self._logger.info(
                "task_created",
                extra={
                    "msg": "task created",
                    "data": {
                        "task_id": task.task_id,
                        "workspace_id": task.workspace_id,
                        "agent_id": self._agent_profile.agent_id,
                    },
                },
            )
        finally:
            reset_log_context(token)
        if self._run_store is not None:
            token = set_log_context(context)
            try:
                run = self._run_store.create_for_turn(task.task_id, task.latest_turn_id or task.task_id, "created")
            finally:
                reset_log_context(token)
            context = self._trace_context_for_task(task.task_id, run.run_id)
            if self._trace_recorder is not None:
                self._trace_recorder.record_event(
                    context,
                    "run_created",
                    {"status": run.status, "thread_id": run.thread_id},
                    source="runtime",
                )
            self._logger.info(
                "durable_run_bound",
                extra=trace_log_extra(
                    context,
                    msg="durable run bound",
                    data={
                        "task_id": task.task_id,
                        "turn_id": run.turn_id,
                        "run_id": run.run_id,
                        "thread_id": run.thread_id,
                    },
                ),
            )
        return task

    def create_turn(self, task_id: str, input_text: str) -> TurnRecord:
        """Create a pending turn for an existing task."""

        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        turn = self._task_store.create_turn(task_id, input_text, "pending")
        if self._run_store is not None:
            run = self._run_store.create_for_turn(task_id, turn.turn_id, "created")
            if self._trace_recorder is not None:
                self._trace_recorder.record_event(
                    self._trace_context_for_task(task_id, run.run_id),
                    "run_created",
                    {"status": run.status, "thread_id": run.thread_id, "turn_id": turn.turn_id},
                    source="runtime",
                )
        self._logger.info(
            "turn_created",
            extra=trace_log_extra(
                self._trace_context_for_task(task_id),
                msg="turn created",
                data={"task_id": task_id, "turn_id": turn.turn_id},
            ),
        )
        return turn

    def list_turns(self, task_id: str) -> list[TurnRecord]:
        """List turns for a task."""

        return self._task_store.list_turns_for_task(task_id)

    def get_turn(self, turn_id: str) -> TurnRecord:
        """Return a turn by identifier."""

        return self._task_store.get_turn(turn_id)

    def cancel_task(self, task_id: str) -> TaskRecord:
        """Cancel a task and mark associated runs as cancelled."""

        runs = self._run_store.list_by_task(task_id) if self._run_store is not None else []
        run = runs[-1] if runs else None
        token = set_log_context(self._trace_context_for_task(task_id, run.run_id if run is not None else ""))
        try:
            task = self._task_store.update_status(task_id, "cancelled")
            for item in runs:
                self._mark_run_for_turn(item.turn_id, "cancelled", interruption_reason="task_cancelled")
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
            reset_log_context(token)


    async def run_task(self, task_id: str) -> AsyncIterator[RuntimeEvent]:
        """Run the latest turn for a task."""

        turn = self._task_store.get_turn_for_task(task_id)
        async for event in self.run_turn(turn.turn_id):
            yield event

    async def run_turn(self, turn_id: str, turn: Optional[TurnRecord] = None) -> AsyncIterator[RuntimeEvent]:
        """Run or replay a specific turn."""

        if turn is None:
            turn = self._task_store.get_turn(turn_id)
        task_id = turn.task_id
        task = self._task_store.get_task(task_id)
        if task.status == "cancelled":
            if turn.status != "cancelled":
                self._task_store.update_turn_status(turn.turn_id, "cancelled")
            existing_events = self._task_store.list_events_for_turn(turn.turn_id) or self._task_store.list_events(task.task_id)
            for event in existing_events:
                yield event
            return
        if turn.status == "running":
            async for event in self._stream_running_turn_events(turn.turn_id):
                yield event
            return
        if turn.status != "pending":
            existing_events = self._task_store.list_events_for_turn(turn.turn_id)
            if existing_events:
                for event in existing_events:
                    yield event
                return
            yield self._record(
                EventType.RUN_FAILED,
                task.task_id,
                {"status": turn.status, "error": "turn is not pending", "_turn_id": turn.turn_id},
            )
            return

        agent_profile = self._resolve_task_agent_profile(task)
        if agent_profile is None:
            self._task_store.update_status(task.task_id, "failed")
            self._task_store.update_turn_status(turn.turn_id, "failed")
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

        if not self._task_store.claim_pending_turn(turn.turn_id):
            async for event in self._stream_running_turn_events(turn.turn_id):
                yield event
            return
        self._task_store.update_status(task.task_id, "running")
        self._mark_run_for_turn(turn.turn_id, "running")
        yield self._record(
            EventType.RUN_STARTED,
            task.task_id,
            {"status": "running", "agent": agent_profile.to_dict(), "_turn_id": turn.turn_id},
        )

        run = self._run_store.get_by_turn(turn.turn_id) if self._run_store is not None else None
        operations = RuntimeOperations(
            settings=self._settings,
            task_store=self._task_store,
            context_builder=self._context_builder,
            model_adapter=self._model_adapter,
            tool_scheduler=self._tool_scheduler,
            logger=self._logger,
            agent_profile=agent_profile,
            record_event=self._record,
            current_turn_id=turn.turn_id,
        )

        try:
            async for event in self._workflow.run(task, operations):
                yield event
            final_task = self._task_store.get_task(task.task_id)
            if final_task.status in {"completed", "failed", "cancelled"}:
                self._task_store.update_turn_status(turn.turn_id, final_task.status)
            self._sync_run_with_turn_status(turn.turn_id, final_task)
            return
        except Exception as exc:
            self._task_store.update_status(task.task_id, "failed")
            self._task_store.update_turn_status(turn.turn_id, "failed")
            self._mark_run_for_turn(turn.turn_id, "failed", interruption_reason=str(exc))
            self._task_store.update_steps_status_for_task(
                task.task_id,
                "running",
                "failed",
                str(exc),
            )
            self._logger.exception(
                "task_failed",
                extra=trace_log_extra(
                    self._trace_context_for_task(task.task_id, run.run_id if run is not None else ""),
                    msg="task execution failed",
                    data={"task_id": task.task_id},
                ),
            )
            yield self._record(
                EventType.RUN_FAILED,
                task.task_id,
                {"status": "failed", "error": str(exc), "_turn_id": turn.turn_id},
            )

    async def _stream_running_turn_events(self, turn_id: str) -> AsyncIterator[RuntimeEvent]:
        """Stream events for a turn that another caller is running."""

        seen_event_ids: set[str] = set()
        while True:
            for event in self._task_store.list_events_for_turn(turn_id):
                if event.event_id not in seen_event_ids:
                    seen_event_ids.add(event.event_id)
                    yield event
            latest_turn = self._task_store.get_turn(turn_id)
            if latest_turn.status != "running":
                break
            await asyncio.sleep(0.1)

    def _sync_run_with_turn_status(self, turn_id: str, task: TaskRecord) -> None:
        """Synchronize durable run status from final task state."""

        if task.status in {"completed", "failed", "cancelled"}:
            self._mark_run_for_turn(turn_id, task.status)

    def _mark_run_for_turn(
        self,
        turn_id: str,
        status: str,
        wait_reason: Optional[str] = None,
        active_step_id: Optional[str] = None,
        active_wait_id: Optional[str] = None,
        interruption_reason: Optional[str] = None,
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
                    "data": {"turn_id": turn_id, "target_status": status},
                },
            )


    def list_events(self, task_id: str) -> list:
        """List stored runtime events for a task."""

        return self._task_store.list_events(task_id)

    def list_steps(self, task_id: str) -> list:
        """List stored runtime steps for a task."""

        return self._task_store.list_steps_for_task(task_id)


    def get_task(self, task_id: str) -> TaskRecord:
        """Return a task by identifier."""

        return self._task_store.get_task(task_id)

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

    def trace_query_service(self) -> TraceQueryService:
        """Return the configured trace query service."""

        if self._trace_query_service is None:
            raise RuntimeError("trace query service is not configured")
        return self._trace_query_service

    def log_query_service(self) -> LogQueryService:
        """Return the configured log query service."""

        if self._log_query_service is None:
            raise RuntimeError("log query service is not configured")
        return self._log_query_service

    def logger(self) -> logging.Logger:
        """Return the runtime logger."""

        return self._logger

    def _trace_context_for_task(self, task_id: str, run_id: str = ""):
        """Return a trace context for a task."""

        if self._trace_recorder is not None:
            context = self._trace_recorder.context_for_task(task_id, run_id)
            bind_log_context(context)
            return context
        from app.core.trace.context import TraceContext

        return TraceContext(trace_id="", task_id=task_id, run_id=run_id)

    def _resolve_task_agent_profile(self, task: TaskRecord) -> Optional[AgentProfile]:
        """Resolve the agent profile allowed to execute a task."""

        if task.agent_id == self._agent_profile.agent_id:
            return self._agent_profile
        return None

    def _record(self, event_type: EventType, task_id: str, payload: dict) -> RuntimeEvent:
        """Create, persist, trace, and log a runtime event."""

        payload = dict(payload)
        turn_id = payload.pop("_turn_id", None)
        tool_call_id = payload.get("tool_call_id")
        event = RuntimeEvent(
            event_type=event_type,
            task_id=task_id,
            turn_id=turn_id,
            sequence=self._task_store.next_event_sequence(task_id),
            tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
            payload=payload,
        )
        self._task_store.append_event(event)
        run = self._run_store.get_by_turn(turn_id) if self._run_store is not None and turn_id else None
        if self._trace_recorder is not None:
            context = self._trace_context_for_task(task_id, run.run_id if run is not None else "")
            trace_event_name = runtime_trace_event_name(str(event_type), payload)
            if trace_event_name:
                self._trace_recorder.record_event(
                    context,
                    trace_event_name,
                    {"runtime_event_id": event.event_id, **payload},
                    source="runtime",
                    level=_trace_level_for_runtime_event(event_type, payload),
                )
        self._logger.info(
            "runtime_event",
            extra=trace_log_extra(
                self._trace_context_for_task(task_id, run.run_id if run is not None else ""),
                msg="runtime event recorded",
                data={
                    "task_id": task_id,
                    "run_id": run.run_id if run is not None else "",
                    "event_type": str(event_type),
                    "event_id": event.event_id,
                },
            ),
        )
        return event
def _trace_level_for_runtime_event(event_type: EventType, payload: dict) -> str:
    """Return the trace log level for a runtime event."""

    if event_type == EventType.RUN_FAILED:
        return "error"
    if event_type == EventType.TOOL_CALL_FINISHED and str(payload.get("status") or "") == "error":
        return "error"
    return "info"
