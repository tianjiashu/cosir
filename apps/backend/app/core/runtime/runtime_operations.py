"""Runtime operations exposed to workflow strategies."""

import logging

from app.config.settings import BackendSettings
from app.core.context import TextContextBuilder
from app.core.agents.profile import AgentProfile
from app.models import RuntimeMessage
from app.models import TaskRecord
from app.models import TurnRecord
from app.tools.schemas import ToolCall
from app.service.tool_execution.run_result import ToolRunResult
from app.tools.tool_execute.tool_scheduler import ToolScheduler


class RuntimeOperations:
    """Expose runtime-owned side effects through a narrow workflow boundary."""

    def __init__(
        self,
        settings: BackendSettings,
        task_store,
        context_builder: TextContextBuilder,
        tool_scheduler: ToolScheduler,
        logger: logging.Logger,
        agent_profile: AgentProfile,
        current_turn_id: str = "",
    ) -> None:
        """Initialize runtime dependencies."""

        self.settings = settings
        self._task_store = task_store
        self._context_builder = context_builder
        self._logger = logger
        self._agent_profile = agent_profile
        self._tool_service = ToolExecutionService(
            scheduler=tool_scheduler,
            allows_tool=lambda tool: agent_profile.allows_tool(
                tool.name,
                tool.permission,
            ),
            agent_id=agent_profile.agent_id,
            logger=logger,
        )
        self._current_turn_id = current_turn_id

    def get_turn_for_task(self, task_id: str) -> TurnRecord:
        """Return the active or latest turn for a task."""

        if self._current_turn_id:
            turn = self._task_store.get_turn(self._current_turn_id)
            if turn.task_id != task_id:
                raise KeyError(task_id)
            return turn
        return self._task_store.get_turn_for_task(task_id)

    def build_messages(self, task: TaskRecord) -> list[RuntimeMessage]:
        """Build model-independent runtime messages for a task."""

        turn = self.get_turn_for_task(task.task_id)
        turn_history = self._task_store.list_turns_for_task(task.task_id)
        return self._context_builder.build_messages(task, self._agent_profile, turn, turn_history)

    def run_tool_calls(
        self,
        task_id: str,
        calls: list[ToolCall],
        step_id: str | None = None,
        write_event=None,
    ) -> ToolRunResult:
        """Execute model-requested tool calls through the tool system.

        工具生命周期事件通过 ``write_event`` 回调写入 LangGraph 自定义事件流；
        若未提供回调，则静默跳过事件（仅执行工具）。

        参数:
            task_id: 当前任务标识符。
            calls: 模型请求的工具调用列表。
            step_id: 请求这些工具调用的步骤标识符。
            write_event: 可选的运行时事件写入回调。

        返回:
            工具观察结果与供下一步模型使用的消息。
        """

        return self._tool_service.run_calls_with_events(
            task_id=task_id,
            step_id=step_id or "",
            calls=calls,
            write_event=write_event or _noop_write_event,
        )

    def has_task_status(self, task_id: str, status: str) -> bool:
        """Return whether a task currently has the requested status."""

        return self._task_store.has_status(task_id, status)

    def update_task_status(self, task_id: str, status: str) -> TaskRecord:
        """Update task status through the runtime task store."""

        return self._task_store.update_status(task_id, status)

    def log_exception(self, event_name: str, extra: dict | None = None) -> None:
        """Write runtime exception diagnostics."""

        self._logger.exception(event_name, extra=extra or {})


def _noop_write_event(event_type, task_id: str, payload: dict) -> None:
    """默认事件写入回调：静默丢弃（无副作用）。"""

    return None
