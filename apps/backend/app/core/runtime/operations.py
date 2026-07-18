"""Runtime operations exposed to workflow strategies."""

import logging
from typing import AsyncIterator, Callable, List, Optional

from app.core.agents.profile import AgentProfile
from app.config.settings import BackendSettings
from app.context.builder import TextContextBuilder
from app.context.budget import validate_context_budget
from app.events.types import EventType, RuntimeEvent
from app.models.base import ModelDelta, RuntimeMessage, StreamingModelAdapter
from app.core.runtime.model_tools import build_model_tool_definitions
from app.storage.records import StepRecord, TaskRecord, TurnRecord


class RuntimeOperations:
    """Expose runtime-owned side effects through a narrow workflow boundary."""

    def __init__(
        self,
        settings: BackendSettings,
        task_store,
        context_builder: TextContextBuilder,
        model_adapter: StreamingModelAdapter,
        tool_scheduler: ToolScheduler,
        logger: logging.Logger,
        agent_profile: AgentProfile,
        record_event: Callable[[EventType, str, dict], RuntimeEvent],
        current_turn_id: str = "",
    ) -> None:
        """Initialize runtime dependencies."""

        self.settings = settings
        self._task_store = task_store
        self._context_builder = context_builder
        self._model_adapter = model_adapter
        self._tool_scheduler = tool_scheduler
        self._logger = logger
        self._agent_profile = agent_profile
        self._record_event = record_event
        self._current_turn_id = current_turn_id

    def get_turn_for_task(self, task_id: str) -> TurnRecord:
        """Return the active or latest turn for a task."""

        if self._current_turn_id:
            turn = self._task_store.get_turn(self._current_turn_id)
            if turn.task_id != task_id:
                raise KeyError(task_id)
            return turn
        return self._task_store.get_turn_for_task(task_id)

    def build_messages(self, task: TaskRecord) -> List[RuntimeMessage]:
        """Build model-independent runtime messages for a task."""

        turn = self.get_turn_for_task(task.task_id)
        turn_history = self._task_store.list_turns_for_task(task.task_id)
        return self._context_builder.build_messages(task, self._agent_profile, turn, turn_history)

    def create_step(
        self,
        turn_id: str,
        step_type: str,
        status: str,
        input_summary: str,
    ) -> StepRecord:
        """Create a persisted runtime step."""

        return self._task_store.create_step(
            turn_id=turn_id,
            step_type=step_type,
            status=status,
            input_summary=input_summary,
        )

    def update_step(
        self,
        step_id: str,
        status: str,
        output_summary: str = "",
        error: Optional[str] = None,
    ) -> StepRecord:
        """Update a persisted runtime step status."""

        return self._task_store.update_step_status(
            step_id,
            status,
            output_summary=output_summary,
            error=error,
        )

    async def stream_model(self, messages: List[RuntimeMessage]) -> AsyncIterator[ModelDelta]:
        """Stream model deltas for workflow messages."""

        validate_context_budget(messages, self.settings.max_context_chars)
        model_tools = build_model_tool_definitions(self._list_agent_visible_tools())
        async for delta in self._model_adapter.stream(messages, model_tools):
            yield delta

    def execute_tool(self, call: ToolCall, step_id: Optional[str] = None) -> ToolObservation:
        """Execute one model-requested tool call."""

        denied_tool = self._find_agent_denied_model_visible_tool(call.tool_name)
        if denied_tool is not None:
            self._logger.warning(
                "agent_tool_denied",
                extra={
                    "msg": "agent profile denied tool call",
                    "data": {
                        "agent_id": self._agent_profile.agent_id,
                        "tool_name": denied_tool.name,
                        "permission": denied_tool.permission,
                    },
                },
            )
            return ToolObservation(
                tool_name=denied_tool.name,
                status="error",
                content="",
                error=f"agent does not allow tool: {denied_tool.name}",
                permission=denied_tool.permission,
            )
        return self._tool_scheduler.execute(call)

    def execute_tools(self, calls: List[ToolCall], step_id: Optional[str] = None) -> List[ToolObservation]:
        """Execute model-requested tool calls."""

        if len(calls) != 1:
            raise RuntimeError("multiple tool calls require a batch-capable tool scheduler")
        return [self.execute_tool(calls[0], step_id)]

    def _list_agent_visible_tools(self) -> List[ToolDefinition]:
        """Return model-visible tools allowed by the agent profile."""

        return [
            tool
            for tool in self._list_platform_visible_tools()
            if self._agent_profile.allows_tool(tool.name, tool.permission)
        ]

    def _find_agent_denied_model_visible_tool(self, tool_name: str) -> Optional[ToolDefinition]:
        """Return a visible tool denied by the agent profile."""

        for tool in self._list_platform_visible_tools():
            if tool.name == tool_name and not self._agent_profile.allows_tool(
                tool.name,
                tool.permission,
            ):
                return tool
        return None

    def _list_platform_visible_tools(self) -> List[ToolDefinition]:
        """Return model-visible tools exposed by the scheduler."""

        return self._tool_scheduler.list_model_visible_tools()

    def has_task_status(self, task_id: str, status: str) -> bool:
        """Return whether a task currently has the requested status."""

        return self._task_store.has_status(task_id, status)

    def update_task_status(self, task_id: str, status: str) -> TaskRecord:
        """Update task status through the runtime-owned store."""

        return self._task_store.update_status(task_id, status)

    def close_running_steps(self, task_id: str, status: str, error: str) -> int:
        """Close running steps for a task."""

        return self._task_store.update_steps_status_for_task(task_id, "running", status, error)

    def record_event(self, event_type: EventType, task_id: str, payload: dict) -> RuntimeEvent:
        """Persist and return a runtime event for the current turn."""

        return self._record_event(event_type, task_id, {**payload, "_turn_id": self._current_turn_id})


    def log_exception(self, event_name: str, extra: dict | None = None) -> None:
        """Write exception diagnostics through the runtime logger."""

        self._logger.exception(event_name, extra=extra or {})
