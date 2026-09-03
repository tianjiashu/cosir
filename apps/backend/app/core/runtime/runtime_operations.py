"""Runtime operations exposed to workflow strategies."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from app.config.logging.logger import log
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.models import ConversationRunRecord, RuntimeMessage, TaskRecord, WorkspaceRecord
from app.service.conversation_run_message_store import ConversationRunMessageStore
from app.service.depends import get_conversation_run_service
from app.assistant_transport.service.conversation_mutation_writer import ConversationMutationWriter
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

    状态单一事实来源是 ``ConversationRun``：本门面暴露的状态和当前 run 方法都作用于
    run，不再写 task 执行态（task 执行态由最新 run 派生）。
    """

    def __init__(
        self,
        tool_scheduler: ToolScheduler,
        agent_profile: AgentProfile,
        current_run: ConversationRunRecord,
        current_task: TaskRecord,
        current_workspace: WorkspaceRecord,
        model_tools: list[ToolDefinition] | None = None,
        execution_context: ToolExecutionContext | None = None,
        runtime_dependencies: ToolRuntimeDependencies | None = None,
        tool_trace_recorder: ToolTraceRecorder | None = None,
    ) -> None:
        """初始化运行时操作门面及其私有协作者。

        参数:
            run_store: 运行存储（私有协作者，不对外暴露）；逐条落库经其
                消息写入/清理门面，避免 core 直连
                storage 层（分层约束：core → service，service → storage）。
            tool_scheduler: 工具调度器（已按 workspace 边界解析或进程级兜底）。
            agent_profile: 驱动本次 run 的 agent profile。
            current_run: 当前绑定的 Conversation Run 记录（门面状态单一事实来源）。
            current_task: 当前执行的任务记录。
            current_workspace: 当前工作区记录。
            model_tools: 暴露给模型的工具定义列表。
            execution_context: 当前执行的运行时边界；为 None 时 ``run_tool_calls``
                日志不注入 ``workspace_id``。
            runtime_dependencies: 可选的本 run 工具运行期依赖；存在 execution_context 时
                会合并进 ``ToolExecutionContext.runtime_dependencies`` 透传给 handler。
            tool_trace_recorder: 可选的工具调用 trace 记录器（依赖倒置）；为 None 时
                工具执行不产生 trace，行为与集成前一致。
            should_cancel: 当前 run 的取消检查回调；为 None 时退化为状态查询。

        返回:
            无。

        异常:
            无。

        副作用:
            构造 ``ToolExecutionService``、存储执行上下文、构造消息持久化端口
            （``ConversationRunMessageStore``，经 ``message_store`` 属性暴露给 workflow）、
            记初始化日志。
        """

        self._conversation_run_state_service = get_conversation_run_service()
        # 消息持久化端口（service 层适配实现）：暴露给 workflow 供 RuntimeContextManager
        # 注入，使 manager 成为消息读写的唯一事实源。原 append_runtime_message /
        # reset_message_sequence 逐条落库逻辑退役，改由 manager 经本端口落库。
        self._message_store = ConversationRunMessageStore(self._conversation_run_state_service)
        self._conversation_writer = ConversationMutationWriter()
        self.model_tools: list[ToolDefinition] = list(model_tools or [])
        self.agent_profile = agent_profile
        self._current_workspace = current_workspace
        self._current_run = current_run
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
            should_cancel=self.is_current_run_cancelled,
        )

        log.info(
            "runtime_ops_initialized",
            extra={
                "msg": f"运行时操作门面已初始化，agent_id={agent_profile.agent_id}",
                "data": {
                    "agent_id": agent_profile.agent_id,
                    "model_tools_count": len(self.model_tools),
                    "current_run_id": current_run.id if current_run else None,
                    "current_run_bound": bool(current_run),
                },
            },
        )

    def get_current_run(self) -> ConversationRunRecord:
        """Return the Conversation Run identified by ``current_run_id``.

        用于工作流取「当前要跑的 run」。
        """

        return self._current_run

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
        """返回本 run 的消息持久化端口（供 workflow 注入 ``RuntimeContextManager``）。

        返回:
            ``ConversationRunMessageStore`` 适配实例，承载 ``turn_messages`` 读写的依赖倒置端口。
        """
        return self._message_store

    def append_assistant_text(self, text: str) -> None:
        """将模型文本增量直接追加到当前运行的 canonical assistant part。

        参数:
            text: 非空模型文本增量。

        返回:
            无。

        异常:
            ValueError: ``text`` 为空。
            KeyError: 当前运行没有 canonical assistant message。
            PermissionError: 当前运行已不再 active。

        副作用:
            经 ``ConversationMutationWriter`` 原子追加文本并提交 canonical fact。
        """
        self._conversation_writer.append_assistant_text_for_run(
            self._current_task.id,
            self._current_run.id,
            text,
        )

    def append_assistant_reasoning(self, text: str) -> None:
        """将模型 reasoning 增量直接追加到当前运行的 canonical reasoning part。

        参数:
            text: 非空 reasoning 文本增量。

        返回:
            无。

        异常:
            ValueError: ``text`` 为空。
            KeyError: 当前运行没有 canonical assistant message。
            PermissionError: 当前运行已不再 active。

        副作用:
            经 ``ConversationMutationWriter`` 原子追加 reasoning part 并提交 canonical fact。
        """
        self._conversation_writer.append_assistant_part_for_run(
            self._current_task.id,
            self._current_run.id,
            "reasoning",
            text,
        )

    def has_conversation_run_status(self, run_id: int, status: str) -> bool:
        """Return whether a Conversation Run currently has the requested status."""

        has = self._conversation_run_state_service.has_conversation_run_status(run_id, status)
        return has

    def is_current_run_cancelled(self) -> bool:
        """Return whether the currently bound run should stop.

        参数:
            无。

        返回:
            当前 run 已被取消时返回 True，否则返回 False。

        异常:
            无。

        副作用:
            可能调用注入的取消检查回调；无回调时读取 run 状态。
        """
        current_run_id = self._current_run.id
        if cancellation_registry.is_cancelled(current_run_id):
            return True
        if not self._current_run:
            return False
        return self.has_conversation_run_status(current_run_id, "cancelled")

    def complete_run_if_running(
        self, run_id: int, response_text: str
    ) -> ConversationRunRecord | None:
        """Complete the Conversation Run only if it is still running.

        参数:
            run_id: 待完成的 Conversation Run 标识。
            response_text: Agent 最终回复文本。

        返回:
            成功完成时返回更新后的 ConversationRunRecord；run 已被取消/失败/完成时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时同事务写入 completed 状态和回复文本。
        """

        response_len = len(response_text)
        log.info(
            "run_completion_attempted",
            extra={
                "msg": f"尝试完成 running run，run_id={run_id}",
                "data": {"run_id": run_id, "response_length": response_len},
            },
        )
        # 最终文本先写入 canonical assistant part；response_text 不是事实来源。
        if response_text:
            self._conversation_writer.append_assistant_text_for_run(
                self._current_task.id,
                run_id,
                response_text,
            )
        mutation = self._conversation_writer.settle_run(
            run_id,
            "completed",
        )
        if mutation is None:
            return None
        return self._conversation_run_state_service.get_run(run_id)

    def fail_run_if_running(
        self, run_id: int, end_reason: str | None = None
    ) -> ConversationRunRecord | None:
        """Fail the Conversation Run only if it is still running.

        参数:
            run_id: 待失败落定的 Conversation Run 标识。
            end_reason: 可选失败原因。

        返回:
            成功失败落定时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时写入 failed 状态。
        """

        log.info(
            "run_failure_attempted",
            extra={
                "msg": f"尝试将 running run 标记为 failed，run_id={run_id}",
                "data": {"run_id": run_id, "end_reason": end_reason},
            },
        )
        mutation = self._conversation_writer.settle_run(
            run_id,
            "failed",
            end_reason=end_reason,
        )
        if mutation is None:
            return None
        return self._conversation_run_state_service.get_run(run_id)

    def cancel_run_if_running(
        self, run_id: int, end_reason: str = "runtime_cancelled"
    ) -> ConversationRunRecord | None:
        """Cancel the Conversation Run through the canonical writer if it is still active.

        参数:
            run_id: 待取消的 Conversation Run 标识。
            end_reason: 稳定的取消原因。

        返回:
            成功取消时返回更新后的 ConversationRunRecord；终态已由其它路径落定时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            通过 canonical writer 条件事务将运行和助手消息一并标记为 cancelled。
        """
        mutation = self._conversation_writer.settle_run(
            run_id,
            "cancelled",
            end_reason=end_reason,
        )
        if mutation is None:
            return None
        return self._conversation_run_state_service.get_run(run_id)

    def run_tool_calls(
        self,
        task_id: str,
        calls: list[ToolCall],
        step_id: str | None = None,
        running_loop: asyncio.AbstractEventLoop | None = None,
    ) -> ToolRunResult:
        """Execute model-requested tool calls through the tool system.

        工具生命周期通过明确的 callback 写入 canonical conversation facts。门面持有的
        ``execution_context`` 在内部透传给执行链，最终在执行期注入各 handler。

        参数:
            task_id: 当前任务标识符。
            calls: 模型请求的工具调用列表。
            step_id: 请求这些工具调用的步骤标识符。
            running_loop: 异步工具节点提供的事件循环，用于工具执行期输出桥接。
        返回:
            工具观察结果与供下一步模型使用的消息。
        """

        effective_step_id = step_id or "step"
        calls = [
            replace(
                call,
                call_id=call.call_id or f"{self._current_run.id}:{effective_step_id}:{index}",
            )
            for index, call in enumerate(calls)
        ]
        self._pre_process_run(task_id=task_id, calls=calls, step_id=step_id)

        execution_context = self._execution_context
        if running_loop is None:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
        if execution_context is not None and running_loop is not None:
            runtime_dependencies = replace(
                execution_context.runtime_dependencies,
                runtime_event_loop=running_loop,
            )
            execution_context = replace(
                execution_context,
                runtime_dependencies=runtime_dependencies,
            )

        result: ToolRunResult = self._tool_service.run_calls(
            step_id=effective_step_id,
            calls=calls,
            execution_context=execution_context,
            on_tool_call_started=self._record_tool_call_started,
            on_tool_call_finished=self._record_tool_call_finished,
            running_loop=running_loop,
        )

        self._post_process_run(task_id=task_id, step_id=step_id, result=result)

        return result

    def _record_tool_call_started(self, _step_id: str, call: ToolCall) -> None:
        """将工具开始事实写入 canonical conversation。"""
        call_id = call.call_id or call.tool_name
        self._conversation_writer.create_tool_call(
            self._current_task.id,
            call_id,
            call.tool_name,
            call.arguments,
            run_id=self._current_run.id,
        )
        self._conversation_writer.transition_tool_call(
            self._current_task.id,
            call_id,
            "running",
            run_id=self._current_run.id,
        )

    def ensure_tool_calls_pending(self, calls: list[ToolCall]) -> None:
        """Persist all model-selected calls before approval or handler execution."""

        for call in calls:
            self._conversation_writer.create_tool_call(
                self._current_task.id,
                call.call_id or call.tool_name,
                call.tool_name,
                call.arguments,
                run_id=self._current_run.id,
            )

    def mark_tool_calls_requires_action(self, calls: list[ToolCall]) -> None:
        """Mark calls as waiting for the server-side approval decision."""

        for call in calls:
            self._conversation_writer.transition_tool_call(
                self._current_task.id,
                call.call_id or call.tool_name,
                "requires-action",
                run_id=self._current_run.id,
            )

    def request_tool_approval(self, calls: list[ToolCall], step_id: str) -> str:
        """Persist the approval request associated with a tool batch."""

        request_id = f"approval-{self._current_run.id}-{step_id}"
        self._conversation_writer.record_approval_request(
            self._current_task.id,
            request_id,
            {
                "stepId": step_id,
                "toolCalls": [
                    {
                        "toolCallId": call.call_id or call.tool_name,
                        "toolName": call.tool_name,
                        "args": call.arguments,
                    }
                    for call in calls
                ],
            },
            run_id=self._current_run.id,
        )
        return request_id
    def _record_tool_call_finished(self, _step_id: str, observation: object) -> None:
        """将工具完成事实写入 canonical conversation。"""
        call_id = getattr(observation, "tool_call_id", None)
        if not isinstance(call_id, str) or not call_id:
            return
        status = getattr(observation, "status", "error")
        self._conversation_writer.complete_tool_call_by_external_id(
            self._current_task.id,
            call_id,
            getattr(observation, "data", None),
            status=(
                "completed"
                if status == "success"
                else ("cancelled" if status == "cancelled" else "failed")
            ),
            error_text=getattr(observation, "error", None) or getattr(observation, "reason", None),
            run_id=self._current_run.id,
        )

    def cancel_tool_calls(self, calls: list[ToolCall]) -> None:
        """Mark calls skipped before execution as cancelled in canonical facts."""

        for call in calls:
            self._conversation_writer.transition_tool_call(
                self._current_task.id,
                call.call_id or call.tool_name,
                "cancelled",
                run_id=self._current_run.id,
            )

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

    def _pre_process_run(
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

        workspace_id = self._current_workspace.id if self._current_workspace else None

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

    def _post_process_run(
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

        workspace_id = self._current_workspace.id if self._current_workspace else None
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
