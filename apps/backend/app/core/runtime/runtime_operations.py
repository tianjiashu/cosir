"""Runtime operations exposed to workflow strategies."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

import sqlalchemy

from app.config.logging.logger import log
from app.models import RuntimeMessage, TaskRecord, TurnRecord, WorkspaceRecord
from app.models.enums.event_type import EventType
from app.models.payload.runtime_event_payload import RuntimeEventPayload
from app.service.depends import get_runtime_event_bus, get_turn_service
from app.service.tool_execution.run_result import ToolRunResult
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.service.tool_execution.tool_trace_recorder import ToolTraceRecorder
from app.tools.schemas import ToolCall, ToolDefinition, ToolExecutionContext
from app.tools.tool_execute.tool_scheduler import ToolScheduler

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile
    from app.service.task.turn_service import TurnService


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
        tool_trace_recorder: ToolTraceRecorder | None = None,
        should_cancel: Callable[[], bool] | None = None,
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
            tool_trace_recorder: 可选的工具调用 trace 记录器（依赖倒置）；为 None 时
                工具执行不产生 trace，行为与集成前一致。
            should_cancel: 当前 turn 的取消检查回调；为 None 时退化为状态查询。

        返回:
            无。

        异常:
            无。

        副作用:
            构造 ``ToolExecutionService``、存储执行上下文、初始化本 turn 逐条落库
            序号计数器（``_message_sequence = 0``）、记初始化日志。
        """

        self._turn_service = get_turn_service()
        self.model_tools: list[ToolDefinition] = list(model_tools or [])
        self.agent_profile = agent_profile
        self._current_workspace = current_workspace
        self._current_turn = current_turn
        self._current_task = current_task
        self._execution_context = execution_context
        self._should_cancel = should_cancel
        # 本 turn 内逐条落库的序号计数器；operations 每 turn 新建，天然随 turn 重置。
        # 注意：审批 interrupt()/Command(resume=) 在 graph 节点内就地恢复，不会重新走
        # run_agent 入口，因此不会重置本计数器——重置仅发生在「从头重跑整个 turn」场景，
        # 该场景下清掉上一轮残留并重新编号是预期的幂等行为。
        self._message_sequence = 0
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

    def reset_message_sequence(self) -> None:
        """清空当前 turn 的消息轨迹并将逐条落库序号归零（turn 开始执行时调用，保证幂等）。

        配合 ``append_runtime_message`` 使用：turn 启动先调用本方法清空当前 turn 在
        ``turn_messages`` 表的全部残留并复位序号，之后每条消息经 ``append_runtime_message``
        自增序号落库；历史 turn 因按 ``turn_id`` 隔离不受影响，跨轮拼装仍由
        ``RuntimeContext.load_for_task`` 从各 turn 读取实现。

        参数:
            无。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果清理失败（由底层 CRUD 透传）。

        副作用:
            删除当前 turn 在 ``turn_messages`` 表的全部行；``_message_sequence`` 归零。
        """

        if not self._current_turn:
            log.warning(
                "runtime_message_reset_skipped_no_turn",
                extra={"msg": "reset_message_sequence ignored: no bound current_turn"},
            )
            return
        try:
            self._turn_service.clear_turn_messages(self._current_turn.turn_id)
        except sqlalchemy.exc.SQLAlchemyError:
            log.exception(
                "runtime_message_reset_failed",
                extra={
                    "msg": "failed to clear turn messages before sequence reset",
                    "data": {"turn_id": self._current_turn.turn_id},
                },
            )
            raise
        self._message_sequence = 0

    def append_runtime_message(self, message: RuntimeMessage) -> None:
        """逐条持久化一条运行时消息（替代 turn 结束后的批覆盖写入）。

        落库序号由门面内部自增维护，调用方无需关心 ``sequence``；单条写入使每条消息在
        产生时即落库，turn 中途失败也能保留已产生的轨迹。消息落库经 ``self._turn_store``
        门面转交 storage 层，core 不直接接触 storage（分层约束：core → service → storage）；
        工作流节点只调用本方法，不直接接触存储层。

        参数:
            message: 单条模型无关的运行时消息；本轮生命周期内依次落库的是用户提问
                （``role="user"``，在 turn 启动时写入）、模型回复（``role="assistant"``，
                含 ``tool_calls``）、工具观察（``role="tool"``）。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败（捕获后写 error 日志并重新抛出）。

        副作用:
            当前 turn 在 ``turn_messages`` 表追加一行；``_message_sequence`` 自增。
        """

        if not self._current_turn:
            log.warning(
                "runtime_message_append_skipped_no_turn",
                extra={
                    "msg": "append_runtime_message ignored: no bound current_turn",
                    "data": {"role": message.role},
                },
            )
            return
        try:
            self._turn_service.append_turn_message(
                self._current_turn.turn_id, message, self._message_sequence
            )
        except sqlalchemy.exc.SQLAlchemyError:
            log.exception(
                "runtime_message_append_failed",
                extra={
                    "msg": "failed to persist runtime message incrementally",
                    "data": {
                        "turn_id": self._current_turn.turn_id,
                        "sequence": self._message_sequence,
                        "role": message.role,
                    },
                },
            )
            raise
        log.debug(
            "runtime_message_appended",
            extra={
                "msg": f"单条运行时消息已落库，turn_id={self._current_turn.turn_id} "
                f"sequence={self._message_sequence} role={message.role}",
                "data": {
                    "turn_id": self._current_turn.turn_id,
                    "sequence": self._message_sequence,
                    "role": message.role,
                },
            },
        )
        self._message_sequence += 1

    def has_turn_status(self, turn_id: str, status: str) -> bool:
        """Return whether a turn currently has the requested status."""

        has = self._turn_service.has_turn_status(turn_id, status)
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
        if not self._current_turn:
            return False
        return self.has_turn_status(self._current_turn.turn_id, "cancelled")

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
