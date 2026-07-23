"""Runtime operations exposed to workflow strategies."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

from app.config.settings import BackendSettings
from app.core.context.builder import TextContextBuilder
from app.models import RuntimeMessage, TurnRecord
from app.service.tool_execution.run_result import ToolRunResult
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.tools.schemas import ToolCall, ToolDefinition
from app.tools.tool_execute.tool_scheduler import ToolScheduler

if TYPE_CHECKING:
    from app.core.agents.profile import AgentProfile


class RuntimeOperations:
    """Expose runtime-owned side effects through a narrow workflow boundary.

    状态单一事实来源是 ``Turn``：本门面暴露的 ``has_turn_status`` / ``update_turn_status``
    / ``get_current_turn`` 全部作用于 turn，不再写 task 执行态（task 执行态由最新 turn 派生）。
    """

    def __init__(
            self,
            settings: BackendSettings,
            turn_store,
            context_builder: TextContextBuilder,
            tool_scheduler: ToolScheduler,
            agent_profile: AgentProfile,
            current_turn_id: str = "",
            model_tools: List[ToolDefinition] = [],
    ) -> None:
        """Initialize runtime dependencies."""

        self.settings = settings
        self._turn_store = turn_store
        self._context_builder = context_builder
        self.model_tools:List[ToolDefinition] = model_tools
        self.agent_profile = agent_profile
        self._tool_service = ToolExecutionService(
            scheduler=tool_scheduler,
            agent_id=agent_profile.agent_id,
        )
        self._current_turn_id = current_turn_id

    def get_current_turn(self) -> TurnRecord:
        """Return the turn identified by ``current_turn_id``.

        用于工作流取「当前要跑的轮」，避免 ``get_turn_for_task`` 总是返回第一轮的历史 bug。
        """

        if not self._current_turn_id:
            raise KeyError("no current turn id bound to runtime operations")
        return self._turn_store.get_turn(self._current_turn_id)

    def get_turn_for_task(self, task_id: str) -> TurnRecord:
        """Return the latest turn for a task (kept for compatibility)."""

        return self._turn_store.get_latest_turn(task_id)

    def get_latest_turn(self, task_id: str) -> TurnRecord:
        """Return the latest turn for a task."""

        return self._turn_store.get_latest_turn(task_id)

    def list_turns_for_task(self, task_id: str) -> list[TurnRecord]:
        """List all turns of a task in creation order."""

        return self._turn_store.list_turns_for_task(task_id)

    def build_messages(self) -> list[RuntimeMessage]:
        """Build model-independent runtime messages for the current turn.

        完全基于 turn（当前轮 + 前置轮轨迹），不再依赖 task 执行态。
        """

        turn = self.get_current_turn()
        turn_history = self._turn_store.list_turns_for_task(turn.task_id)
        return self._context_builder.build_messages(
            self.agent_profile, turn, turn_history, self._turn_store
        )

    def has_turn_status(self, turn_id: str, status: str) -> bool:
        """Return whether a turn currently has the requested status."""

        return self._turn_store.has_turn_status(turn_id, status)

    def update_turn_status(
            self, turn_id: str, status: str, end_reason: str | None = None
    ) -> TurnRecord:
        """Update turn status (and optional end reason) through the turn store."""

        return self._turn_store.update_turn_status(turn_id, status, end_reason)

    def update_turn_response(
            self, turn_id: str, response_text: str | None
    ) -> TurnRecord:
        """Persist the turn's agent reply text through the turn store."""

        return self._turn_store.update_turn_response(turn_id, response_text)

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


def _noop_write_event(event_type, payload: dict) -> None:
    """默认事件写入回调：静默丢弃（无副作用）。

    签名与运行时实际回调 ``write_event(event_type, payload)`` 保持一致，确保未提供
    ``write_event`` 时作为默认回调传入不会因参数数量不匹配而抛 ``TypeError``。
    """

    return None
