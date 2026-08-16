"""Runtime operations exposed to workflow strategies."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from app.config.logging.logger import log
from app.core.runtime.turn_cancellation_registry import cancellation_registry
from app.models import RuntimeMessage, TaskRecord, TurnRecord, WorkspaceRecord
from app.models.enums.event_type import EventType
from app.models.payload.runtime_event_payload import RuntimeEventPayload
from app.service.depends import get_runtime_event_bus, get_turn_service
from app.service.turn_runtime_message_store import TurnRuntimeMessageStore
from app.service.tool_execution.run_result import ToolRunResult
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.service.tool_execution.tool_trace_recorder import ToolTraceRecorder
from app.tools.schemas import ToolCall, ToolDefinition, ToolExecutionContext
from app.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.tools.tool_execute.tool_scheduler import ToolScheduler

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile
    from app.core.context.runtime_message_store import RuntimeMessageStore


class RuntimeOperations:
    """Expose runtime-owned side effects through a narrow workflow boundary.

    状态单一事实来源是 ``Turn``：本门面暴露的 ``has_turn_status`` / ``update_turn_status``
    / ``get_current_turn`` 全部作用于 turn，不再写 task 执行态（task 执行态由最新 turn 派生）。
    """

    def __init__(
        self,
        tool_scheduler: ToolScheduler,
        agent_profile: AgentProfile,
        current_turn: TurnRecord,
        current_task: TaskRecord,
        current_workspace: WorkspaceRecord,
        model_tools: list[ToolDefinition] | None = None,
        execution_context: ToolExecutionContext | None = None,
        runtime_dependencies: ToolRuntimeDependencies | None = None,
        tool_trace_recorder: ToolTraceRecorder | None = None,
    ) -> None:
        """初始化运行时操作门面及其私有协作者。

        参数:
            turn_store: 轮次存储（私有协作者，不对外暴露）；逐条落库经其
                ``append_turn_message`` / ``clear_turn_messages`` 门面，避免 core 直连
                storage 层（分层约束：core → service，service → storage）。
            tool_scheduler: 工具调度器（已按 workspace 边界解析或进程级兜底）。
            agent_profile: 驱动本轮执行的 agent profile。
            current_turn: 当前绑定的轮次记录（门面状态单一事实来源）。
            current_task: 当前执行的任务记录。
            current_workspace: 当前工作区记录。
            model_tools: 暴露给模型的工具定义列表。
            execution_context: 当前执行的运行时边界；为 None 时 ``run_tool_calls``
                日志不注入 ``workspace_id``。
            runtime_dependencies: 可选的本 turn 工具运行期依赖；存在 execution_context 时
                会合并进 ``ToolExecutionContext.runtime_dependencies`` 透传给 handler。
            tool_trace_recorder: 可选的工具调用 trace 记录器（依赖倒置）；为 None 时
                工具执行不产生 trace，行为与集成前一致。
            should_cancel: 当前 turn 的取消检查回调；为 None 时退化为状态查询。

        返回:
            无。

        异常:
            无。

        副作用:
            构造 ``ToolExecutionService``、存储执行上下文、构造消息持久化端口
            （``TurnRuntimeMessageStore``，经 ``message_store`` 属性暴露给 workflow）、
            记初始化日志。
        """

        self._turn_service = get_turn_service()
        # 消息持久化端口（service 层适配实现）：暴露给 workflow 供 RuntimeContextManager
        # 注入，使 manager 成为消息读写的唯一事实源。原 append_runtime_message /
        # reset_message_sequence 逐条落库逻辑退役，改由 manager 经本端口落库。
        self._message_store = TurnRuntimeMessageStore(self._turn_service)
        self.model_tools: list[ToolDefinition] = list(model_tools or [])
        self.agent_profile = agent_profile
        self._current_workspace = current_workspace
        self._current_turn = current_turn
        self._current_task = current_task
        self._execution_context = (
            replace(execution_context, runtime_dependencies=runtime_dependencies)
            if execution_context is not None and runtime_dependencies is not None
            else execution_context
        )
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
                    "current_turn_id": current_turn.turn_id if current_turn else None,
                    "current_turn_bound": bool(current_turn),
                },
            },
        )

    def get_current_turn(self) -> TurnRecord:
        """Return the turn identified by ``current_turn_id``.

        用于工作流取「当前要跑的轮」.
        """

        return self._current_turn

    def get_current_task(self) -> TaskRecord:
        """Return the task identified by ``current_task_id``.

        用于工作流取「当前要跑的任务」.
        """

        return self._current_task

    def get_current_workspace(self) -> WorkspaceRecord:
        """Return the workspace identified by ``current_workspace_id``.

        用于工作流取「当前要跑的任务」.
        """

        return self._current_workspace

    @property
    def message_store(self) -> RuntimeMessageStore:
        """返回本 turn 的消息持久化端口（供 workflow 注入 ``RuntimeContextManager``）。

        返回:
            ``TurnRuntimeMessageStore`` 适配实例，承载 ``turn_messages`` 读写的依赖倒置端口。
        """
        return self._message_store

    def has_turn_status(self, turn_id: str, status: str) -> bool:
        """Return whether a turn currently has the requested status."""

        has = self._turn_service.has_turn_status(turn_id, status)
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
        current_turn_id = self._current_turn.turn_id
        if cancellation_registry.is_cancelled(current_turn_id):
            return True
        if not self._current_turn:
            return False
        return self.has_turn_status(current_turn_id, "cancelled")

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
        return self._turn_service.complete_turn_if_running(turn_id, response_text)

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
        return self._turn_service.fail_turn_if_running(turn_id, end_reason)

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

        execution_context = self._execution_context
        if execution_context is not None and running_loop is not None:
            runtime_dependencies = replace(
                execution_context.runtime_dependencies,
                runtime_event_loop=running_loop,
            )
            execution_context = replace(
                execution_context,
                runtime_dependencies=runtime_dependencies,
            )

        result: ToolRunResult = self._tool_service.run_calls_with_events(
            step_id=step_id or "",
            calls=calls,
            execution_context=execution_context,
            write_event=write_event,
            running_loop=running_loop,
        )

        self._post_process_turn(task_id=task_id, step_id=step_id, result=result)

        return result

    def build_cancel_placeholder_messages(
        self,
        calls: list[ToolCall],
    ) -> list[RuntimeMessage]:
        """为执行前已确定取消的工具调用构造配对闭合占位消息。

        转发到 ``ToolExecutionService.build_cancel_placeholder_messages``，使 ``core``
        编排层无需钻入 ``_tool_service`` 受保护成员即可复用 service 的取消占位实现，
        保证执行前整批取消分支与正常执行路径（含执行中取消）产出的协议字段完全一致。

        参数:
            calls: 已确定不会执行的工具调用列表。

        返回:
            按入参顺序排列、可直接写回运行时上下文的 ``role="tool"`` 消息列表。

        异常:
            无。

        副作用:
            无（仅委托 service 构造消息）。
        """
        return self._tool_service.build_cancel_placeholder_messages(calls)

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

        workspace_id = self._current_workspace.workspace_id if self._current_workspace else None

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

        workspace_id = self._current_workspace.workspace_id if self._current_workspace else None
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
