"""Runtime operations exposed to workflow strategies."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.config.logging.logger import log
from app.core.context.runtime_context_builder import RuntimeContextBuilder
from app.models import RuntimeMessage, TurnRecord
from app.models.enums.event_type import EventType
from app.models.payload.runtime_event_payload import RuntimeEventPayload
from app.service.depends import get_runtime_event_bus
from app.service.tool_execution.run_result import ToolRunResult
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.service.tool_execution.tool_trace_recorder import ToolTraceRecorder
from app.tools.schemas import ToolCall, ToolDefinition, ToolExecutionContext
from app.tools.tool_execute.tool_scheduler import ToolScheduler

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile


class RuntimeOperations:
    """Expose runtime-owned side effects through a narrow workflow boundary.

    状态单一事实来源是 ``Turn``：本门面暴露的 ``has_turn_status`` / ``update_turn_status``
    / ``get_current_turn`` 全部作用于 turn，不再写 task 执行态（task 执行态由最新 turn 派生）。
    """

    def __init__(
        self,
        turn_store,
        context_builder: RuntimeContextBuilder,
        tool_scheduler: ToolScheduler,
        agent_profile: AgentProfile,
        current_turn_id: str = "",
        model_tools: list[ToolDefinition] | None = None,
        execution_context: ToolExecutionContext | None = None,
        tool_trace_recorder: ToolTraceRecorder | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        """初始化运行时操作门面及其私有协作者。

        参数:
            turn_store: 轮次存储（私有协作者，不对外暴露）。
            context_builder: 文本上下文构建器。
            tool_scheduler: 工具调度器（已按 workspace 边界解析或进程级兜底）。
            agent_profile: 驱动本轮执行的 agent profile。
            current_turn_id: 当前绑定的轮次标识；空串表示尚未绑定。
            model_tools: 暴露给模型的工具定义列表。
            execution_context: 当前执行的运行时边界；为 None 时 ``run_tool_calls``
                日志不注入 ``workspace_id``。
            tool_trace_recorder: 可选的工具调用 trace 记录器（依赖倒置）；为 None 时
                工具执行不产生 trace，行为与集成前一致。
            should_cancel: 当前 turn 的取消检查回调；为 None 时退化为状态查询。

        返回:
            无。

        异常:
            无。

        副作用:
            构造 ``ToolExecutionService``、存储执行上下文、记初始化日志。
        """

        self._turn_store = turn_store
        self._context_builder = context_builder
        self.model_tools: list[ToolDefinition] = list(model_tools or [])
        self.agent_profile = agent_profile
        self._current_turn_id = current_turn_id
        self._execution_context = execution_context
        self._should_cancel = should_cancel
        self._tool_service = ToolExecutionService(
            scheduler=tool_scheduler,
            agent_id=agent_profile.agent_id,
            allowed_tool_names=(tool.name for tool in self.model_tools),
            tool_definitions=self.model_tools,
            trace_recorder=tool_trace_recorder,
            should_cancel=self.is_current_turn_cancelled,
            event_bus=get_runtime_event_bus(),
        )

        log.info(
            "runtime_ops_initialized",
            extra={
                "msg": f"运行时操作门面已初始化，agent_id={agent_profile.agent_id}",
                "data": {
                    "agent_id": agent_profile.agent_id,
                    "model_tools_count": len(self.model_tools),
                    "current_turn_id": current_turn_id,
                    "current_turn_bound": bool(current_turn_id),
                },
            },
        )

    def get_current_turn(self) -> TurnRecord:
        """Return the turn identified by ``current_turn_id``.

        用于工作流取「当前要跑的轮」，避免 ``get_turn_for_task`` 总是返回第一轮的历史 bug。
        """

        if not self._current_turn_id:
            log.error(
                "current_turn_id_missing",
                extra={
                    "msg": "runtime operations 未绑定 current_turn_id，无法解析当前轮",
                    "data": {"agent_id": self.agent_profile.agent_id},
                },
            )
            raise KeyError("no current turn id bound to runtime operations")
        log.debug(
            "current_turn_resolved",
            extra={
                "msg": f"解析当前轮，turn_id={self._current_turn_id}",
                "data": {
                    "agent_id": self.agent_profile.agent_id,
                    "turn_id": self._current_turn_id,
                },
            },
        )
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
        try:
            turn = self.get_current_turn()
            turn_history = self._turn_store.list_turns_for_task(turn.task_id)
            messages = self._context_builder.build_messages(
                self.agent_profile,
                turn,
                turn_history,
                self._turn_store,
                execution_context=self._execution_context,
            )
            log.info(
                "messages_built",
                extra={
                    "msg": (
                        f"已为 turn_id={turn.turn_id} 构建模型消息，"
                        f"共 {len(messages)} 条（含 {len(turn_history)} 轮历史）"
                    ),
                    "data": {
                        "turn_id": turn.turn_id,
                        "task_id": turn.task_id,
                        "message_count": len(messages),
                        "history_turn_count": len(turn_history),
                    },
                },
            )
            return messages
        except Exception:
            log.exception(
                "messages_build_failed",
                extra={
                    "msg": f"构建运行时消息失败，turn_id={self._current_turn_id}",
                    "data": {"turn_id": self._current_turn_id},
                },
            )
            return []

    def has_turn_status(self, turn_id: str, status: str) -> bool:
        """Return whether a turn currently has the requested status."""

        has = self._turn_store.has_turn_status(turn_id, status)
        log.debug(
            "turn_status_checked",
            extra={
                "msg": f"检查 turn_id={turn_id} 是否处于 {status} 状态：{has}",
                "data": {"turn_id": turn_id, "status": status, "has_status": has},
            },
        )
        return has

    def is_current_turn_cancelled(self) -> bool:
        """Return whether the currently bound turn should stop.

        参数:
            无。

        返回:
            当前 turn 已被取消时返回 True，否则返回 False。

        异常:
            无。

        副作用:
            可能调用注入的取消检查回调；无回调时读取 turn 状态。
        """

        if self._should_cancel is not None and self._should_cancel():
            return True
        if not self._current_turn_id:
            return False
        return self.has_turn_status(self._current_turn_id, "cancelled")

    def update_turn_status(
        self, turn_id: str, status: str, end_reason: str | None = None
    ) -> TurnRecord:
        """Update turn status (and optional end reason) through the turn store."""

        log.info(
            "turn_status_updated",
            extra={
                "msg": f"更新 turn_id={turn_id} 状态为 {status}",
                "data": {
                    "turn_id": turn_id,
                    "status": status,
                    "end_reason": end_reason,
                },
            },
        )
        return self._turn_store.update_turn_status(turn_id, status, end_reason)

    def complete_turn_if_running(self, turn_id: str, response_text: str) -> TurnRecord | None:
        """Complete the turn only if it is still running.

        参数:
            turn_id: 待完成的 turn 标识。
            response_text: Agent 最终回复文本。

        返回:
            成功完成时返回更新后的 TurnRecord；turn 已被取消/失败/完成时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时同事务写入 completed 状态和回复文本。
        """

        response_len = len(response_text)
        log.info(
            "turn_completion_attempted",
            extra={
                "msg": f"尝试完成 running turn，turn_id={turn_id}",
                "data": {"turn_id": turn_id, "response_length": response_len},
            },
        )
        return self._turn_store.complete_turn_if_running(turn_id, response_text)

    def fail_turn_if_running(
        self, turn_id: str, end_reason: str | None = None
    ) -> TurnRecord | None:
        """Fail the turn only if it is still running.

        参数:
            turn_id: 待失败落定的 turn 标识。
            end_reason: 可选失败原因。

        返回:
            成功失败落定时返回更新后的 TurnRecord；turn 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时写入 failed 状态。
        """

        log.info(
            "turn_failure_attempted",
            extra={
                "msg": f"尝试将 running turn 标记为 failed，turn_id={turn_id}",
                "data": {"turn_id": turn_id, "end_reason": end_reason},
            },
        )
        return self._turn_store.fail_turn_if_running(turn_id, end_reason)

    def update_turn_response(self, turn_id: str, response_text: str | None) -> TurnRecord:
        """Persist the turn's agent reply text through the turn store."""

        response_len = len(response_text) if response_text else 0
        log.info(
            "turn_response_updated",
            extra={
                "msg": f"更新 turn_id={turn_id} 的 agent 回复文本，长度 {response_len}",
                "data": {
                    "turn_id": turn_id,
                    "response_length": response_len,
                },
            },
        )
        return self._turn_store.update_turn_response(turn_id, response_text)

    def run_tool_calls(
        self,
        task_id: str,
        calls: list[ToolCall],
        step_id: str | None = None,
        write_event: Callable[[EventType, RuntimeEventPayload], None] | None = None,
        running_loop: asyncio.AbstractEventLoop | None = None,
    ) -> ToolRunResult:
        """Execute model-requested tool calls through the tool system.

        工具生命周期事件通过 ``write_event`` 回调写入 LangGraph 自定义事件流；
        若未提供回调，则静默跳过事件（仅执行工具）。门面持有的 ``execution_context``
        在内部透传给执行链，最终在执行期注入各 handler（便于后续扩展执行参数）。

        参数:
            task_id: 当前任务标识符。
            calls: 模型请求的工具调用列表。
            step_id: 请求这些工具调用的步骤标识符。
            write_event: 可选的运行时事件写入回调。
            running_loop: 承载本轮运行的事件循环；本方法常被异步节点经
                ``asyncio.to_thread`` 调度到工作线程执行，故由调用方传入，
                用于把命令运行期输出增量广播调度回循环线程。缺省时禁用该实时通道。

        返回:
            工具观察结果与供下一步模型使用的消息。
        """

        self._pre_process_turn(task_id=task_id, calls=calls, step_id=step_id)

        result: ToolRunResult = self._tool_service.run_calls_with_events(
            step_id=step_id or "",
            calls=calls,
            execution_context=self._execution_context,
            write_event=write_event,
            running_loop=running_loop,
        )

        self._post_process_turn(task_id=task_id, step_id=step_id, result=result)

        return result

    def _pre_process_turn(
        self,
        task_id: str,
        calls: list[ToolCall],
        step_id: str | None = None,
    ) -> None:
        """Log metadata before dispatching a tool-call batch.

        参数:
            task_id: 当前任务标识。
            calls: 待派发的工具调用列表。
            step_id: 可选步骤标识。

        返回:
            无。

        异常:
            无。

        副作用:
            写入工具批次派发日志。
        """

        workspace_id = (
            self._execution_context.workspace_id if self._execution_context is not None else None
        )

        log.info(
            "tool_calls_dispatched",
            extra={
                "msg": f"派发 {len(calls)} 个工具调用，step_id={step_id}",
                "data": {
                    "task_id": task_id,
                    "step_id": step_id,
                    "call_count": len(calls),
                    "tool_names": [call.tool_name for call in calls],
                    "workspace_id": workspace_id,
                },
            },
        )

    def _post_process_turn(
        self,
        task_id: str,
        result: ToolRunResult,
        step_id: str | None = None,
    ) -> None:
        """Log metadata after a tool-call batch completes.

        参数:
            task_id: 当前任务标识。
            result: 工具批次执行结果。
            step_id: 可选步骤标识。

        返回:
            无。

        异常:
            无。

        副作用:
            写入工具批次完成日志。
        """

        workspace_id = (
            self._execution_context.workspace_id if self._execution_context is not None else None
        )
        status_counts: dict[str, int] = {}
        error_count = 0
        for obs in result.observations:
            status_counts[obs.status] = status_counts.get(obs.status, 0) + 1
            if obs.status == "error":
                error_count += 1

        log.info(
            "tool_calls_completed",
            extra={
                "msg": (
                    f"工具批次执行完成：{len(result.observations)} 个观察，"
                    f"其中 {error_count} 个失败"
                ),
                "data": {
                    "task_id": task_id,
                    "step_id": step_id,
                    "observation_count": len(result.observations),
                    "messages_for_model_count": len(result.messages_for_model),
                    "status_counts": status_counts,
                    "error_count": error_count,
                    "workspace_id": workspace_id,
                },
            },
        )
