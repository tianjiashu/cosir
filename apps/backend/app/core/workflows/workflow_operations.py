"""Runtime operations exposed to workflow strategies."""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import TYPE_CHECKING

from langchain_core.messages import ToolMessage

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.observability.tool_trace_recorder import (
    ToolTraceRecorder,
    _NullToolTraceRecorder,
)
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import ToolCall, ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.core.tools.tool_execute.tool_error import (
    internal_execution_error_reason,
    tool_error,
)
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord
from app.models.enums.error_kind import ErrorKind
from app.service.depends import (
    get_conversation_event_projector,
    get_conversation_run_service,
)

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile


class WorkflowOperations:
    """Expose runtime-owned side effects through a narrow workflow boundary.

    状态单一事实来源是 ``ConversationRun``：本门面暴露的状态和当前 run 方法都作用于
    run，不再写 task 执行态（task 执行态由最新 run 派生）。
    """

    def __init__(
        self,
        tool_executor: ToolExecutor,
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
            tool_executor: 工具执行管线（已按 workspace 边界解析或进程级兜底）。
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

        返回:
            无。

        异常:
            无。

        副作用:
            构造工具执行所需的私有协作者（执行器、可见工具集、并行模式、trace 记录器、
            取消回调），存储执行上下文与 canonical writer，记初始化日志。
        """

        self._conversation_run_service = get_conversation_run_service()
        self._event_projector = get_conversation_event_projector()
        self._executor = tool_executor
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
        self._allowed_tool_names = frozenset(tool.name for tool in self.model_tools)
        self._parallel_mode_by_name = {
            definition.name: definition.parallel_mode for definition in self.model_tools
        }
        self._trace_recorder = tool_trace_recorder or _NullToolTraceRecorder()
        self._should_cancel = self.is_current_run_cancelled

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

    def process_event(self, event: object) -> object | None:
        """把 workflow event 交给唯一的 snapshot projector。

        projector 属于 Transport 旁路；workflow 的 canonical facts 已由各自的持久化
        owner 写入，projector 失败只能记录降级，不能把异常传播回工作流节点。
        """

        try:
            return self._event_projector.process(event)
        except Exception:
            log.exception(
                "workflow_event_projector_failed",
                extra={
                    "msg": "workflow Transport event projector 失败，已降级继续执行",
                    "data": {
                        "event_type": getattr(event, "type", type(event).__name__),
                        "task_id": getattr(event, "task_id", None),
                        "run_id": getattr(event, "run_id", None),
                    },
                },
            )
            return None

    def is_current_run_cancelled(self) -> bool:
        """Return whether the currently bound run should stop.

        取消检测只读进程内取消注册表（运行时信号源）：取消入口由
        ``ConversationRunExecutor.cancel`` 统一标记信号并落库，本方法不做 DB 兜底查询，
        避免协作取消检查在 process 模式工具的 50ms 轮询中产生高频数据库读。

        参数:
            无。

        返回:
            当前 run 已被取消时返回 True；未绑定 run 或无取消信号时返回 False。

        异常:
            无。

        副作用:
            无。
        """
        current_run = self._current_run
        if current_run is None:
            return False
        return cancellation_registry.is_cancelled(str(current_run.id))

    def complete_run_if_running(
        self,
        usage_stats: ConversationRunUsageStats | None = None,
        final_output: str | None = None,
    ) -> ConversationRunRecord | None:
        """Complete the Conversation Run only if it is still running.

        参数:
            usage_stats: 可选的运行用量统计，透传给 ConversationRunService 以发布带用量的状态事件。
            final_output: 可选，Agent 对该轮次的最终回答文本，随终态一并写入 run 行。

        返回:
            成功完成时返回更新后的 ConversationRunRecord；run 已被取消/失败/完成时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时同事务写入 completed 状态、回复文本与可选的最终回答；状态变更事件已由
            ConversationRunService 在数据库更新成功后统一发布，本方法不再直接处理事件。
        """
        run_id = self._current_run.id
        log.info(
            "run_completion_attempted",
            extra={
                "msg": f"尝试完成 running run，run_id={run_id}",
                "data": {"run_id": run_id},
            },
        )
        record = self._conversation_run_service.complete_run_if_running(
            run_id, final_output=final_output, usage_stats=usage_stats
        )
        return record

    def fail_run_if_running(
        self,
        end_reason: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
        final_output: str | None = None,
    ) -> ConversationRunRecord | None:
        """Fail the Conversation Run only if it is still running.

        终态同时写入 ``final_output``，使复用同一工作流的子 Agent 即便失败，主 Agent 也能从
        委派结果中感知其终态输出，而非仅看到一个空终态。

        参数:
            end_reason: 可选失败原因。
            usage_stats: 可选 token 使用统计，随状态事件透出。
            final_output: 可选，随终态一并写入的失败说明文本，供委派场景主 Agent 感知。

        返回:
            成功失败落定时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时写入 failed 状态与 final_output；状态变更事件已由 ConversationRunService
            在数据库更新成功后统一发布，本方法不再直接处理事件。
        """
        run_id = self._current_run.id
        log.info(
            "run_failure_attempted",
            extra={
                "msg": f"尝试将 running run 标记为 failed，run_id={run_id}",
                "data": {"run_id": run_id, "end_reason": end_reason},
            },
        )
        record = self._conversation_run_service.fail_run_if_running(
            run_id, end_reason, final_output=final_output, usage_stats=usage_stats
        )
        return record

    def cancel_run_if_running(
        self,
        end_reason: str = "runtime_cancelled",
        usage_stats: ConversationRunUsageStats | None = None,
        final_output: str | None = None,
    ) -> ConversationRunRecord | None:
        """Cancel the Conversation Run through the canonical writer if it is still active.

        终态同时写入 ``final_output``，使复用同一工作流的子 Agent 即便被取消，主 Agent 也能从
        委派结果中感知其已产出（或被中断）的内容，而非仅看到一个空终态。

        参数:
            end_reason: 稳定的取消原因。
            usage_stats: 可选 token 使用统计，随状态事件透出。
            final_output: 可选，随终态一并写入的取消说明/部分输出文本，供委派场景主 Agent 感知。

        返回:
            成功取消时返回更新后的 ConversationRunRecord；终态已由其它路径落定时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            通过 canonical writer 条件事务将运行和助手消息一并标记为 cancelled 与 final_output；
            状态变更事件由 ConversationRunService 在数据库更新成功后发布；本方法不再直接处理事件。
        """
        run_id = self._current_run.id
        log.info(
            "run_cancel_attempted",
            extra={
                "msg": f"尝试将 running run 标记为 cancelled，run_id={run_id}",
                "data": {"run_id": run_id, "end_reason": end_reason},
            },
        )
        record = self._conversation_run_service.cancel_run_if_running(
            run_id, end_reason, final_output=final_output, usage_stats=usage_stats
        )
        return record

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


        serial_calls: list[tuple[int, ToolCall]] = []
        parallel_calls: list[tuple[int, ToolCall]] = []
        for index, call in enumerate(calls):
            if self._is_parallel_call(call):
                parallel_calls.append((index, call))
            else:
                serial_calls.append((index, call))

        indexed_observations: list[tuple[int, ToolObservation]] = []
        for index, call in serial_calls:
            if self._should_cancel():
                break
            observation = self._execute_tool_call(task_id, call, step_id)
            indexed_observations.append((index, observation))

        if parallel_calls and not self._should_cancel():
            indexed_observations.extend(
                self._run_calls_with_parallel_modes(task_id, parallel_calls, step_id)
            )

        executed_observations = [observation for _, observation in indexed_observations]
        return ToolRunResult(observations=executed_observations)

    def _run_calls_with_parallel_modes(
        self,
        task_id: str,
        calls: list[tuple[int, ToolCall]],
        step_id: str | None,
    ) -> list[tuple[int, ToolObservation]]:
        """并发执行一批已声明为可并行调度的工具调用。

        只处理并行组：``run_tool_calls`` 入口已按工具声明的调度模式完成分流，本方法不再
        包含串行分支。每次线程池提交前用 ``contextvars.copy_context()`` 复制当前线程
        context（含父 turn 根 observation 的 OTel current span），并经 ``ctx.run`` 包装提交，
        使 worker 线程在捕获的 context 里执行工具——并行工具（尤其 ``delegate_task``）的
        tool observation 与子 turn 由此正确嵌套在父 turn trace 下。

        参数:
            task_id: 当前任务标识符，仅用于日志与追踪。
            calls: 带原始位置的并行工具调用列表（元组 ``(index, call)``）。
            step_id: 请求这些工具调用的步骤标识符。

        返回:
            带原始位置的已执行观察列表（按实际完成顺序）。批次中途取消时可能少于传入数量。

        异常:
            无。worker 抛出的意外异常会被收口为对应 call 的 error 观察。

        副作用:
            启动临时线程池执行工具；每任务复制一份 contextvars 快照，不引入跨线程可变状态。
        """
        if not calls or self._should_cancel():
            return []

        completed: list[tuple[int, ToolObservation]] = []
        max_workers = min(len(calls), Settings.MAX_PARALLEL_TOOL_CALLS)
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="tool-parallel",
        ) as pool:
            pending_calls = list(calls)
            future_by_call: dict[Future[ToolObservation], tuple[int, ToolCall]] = {}

            def _submit_until_full() -> None:
                """提交待执行调用，直到达到 worker 上限或检测到取消。"""
                while (
                    pending_calls
                    and len(future_by_call) < max_workers
                    and not self._should_cancel()
                ):
                    batch_index, batch_call = pending_calls.pop()
                    run_ctx = contextvars.copy_context()
                    future_by_call[
                        pool.submit(  # pyright: ignore
                            run_ctx.run,
                            self._execute_tool_call,
                            task_id,
                            batch_call,
                            step_id,
                        )
                    ] = (batch_index, batch_call)

            _submit_until_full()
            while future_by_call:
                done_futures, _ = wait(future_by_call, return_when=FIRST_COMPLETED)
                for future in done_futures:
                    index, call = future_by_call.pop(future)
                    try:
                        observation = future.result()
                    except Exception as exc:  # pragma: no cover - 防御性收口
                        observation = self._internal_error_observation(task_id, call, exc, step_id)
                    completed.append((index, observation))
                _submit_until_full()
        return completed

    def _execute_tool_call(
        self,
        task_id: str,
        call: ToolCall,
        step_id: str | None,
    ) -> ToolObservation:
        """执行单个工具调用，并把执行链路异常收口为工具观察。

        参数:
            task_id: 当前任务标识符，仅用于日志与追踪。
            call: 当前工具调用。
            step_id: 请求该工具调用的步骤标识符。

        返回:
            执行器返回的观察，或内部异常对应的 error 观察。

        异常:
            无。内部异常在本方法内转为 ``ToolObservation``。

        副作用:
            调用底层 ``ToolExecutor``，并记录可选 trace span。
        """
        try:
            with self._trace_recorder.span(call, step_id or "") as tool_span:
                observation = self._executor.execute(
                    call,
                    execution_context=self._execution_context,
                    allowed_tool_names=self._allowed_tool_names,
                    should_cancel=self._should_cancel,
                )
                tool_span.record(observation)
                return observation
        except Exception as exc:
            return self._internal_error_observation(task_id, call, exc, step_id)

    def _internal_error_observation(
        self,
        task_id: str,
        call: ToolCall,
        exc: Exception,
        step_id: str | None = None,
    ) -> ToolObservation:
        """把工具执行链路内部异常转换为稳定的 error 观察。

        参数:
            task_id: 当前任务标识符，仅用于日志与追踪。
            call: 当前工具调用。
            exc: 被捕获的执行链路异常。
            step_id: 请求该工具调用的步骤标识符。

        返回:
            面向模型的内部错误观察。

        异常:
            无。

        副作用:
            写入带堆栈的 error 日志。
        """
        log.error(
            "tool_call_internal_error",
            extra={
                "msg": "工具调用执行链内部异常，已收口为 error 观察",
                "data": {
                    "error_kind": ErrorKind.RUNTIME_FAILED.value,
                    "tool_name": call.tool_name,
                    "tool_call_id": call.call_id,
                    "task_id": task_id,
                    "step_id": step_id,
                    "error": str(exc),
                },
            },
            exc_info=True,
        )
        header = f"internal execution error before the tool ran: {type(exc).__name__}"
        return tool_error(
            tool_name=call.tool_name,
            error=header,
            reason=internal_execution_error_reason(header),
            retryable=False,
            tool_call_id=call.call_id,
        )

    def _is_parallel_call(self, call: ToolCall) -> bool:
        """判断工具调用是否声明为可并行调度。

        参数:
            call: 当前工具调用。

        返回:
            工具定义存在且 ``parallel_mode`` 为 ``"parallel"`` 时返回 ``True``。

        异常:
            无。

        副作用:
            无。
        """
        return self._parallel_mode_by_name.get(call.tool_name, "serial") == "parallel"

    def to_tool_model_message(self, observation: ToolObservation) -> ToolMessage:
        """把已治理的工具观察转为模型上下文消息（``observe`` 节点的共享契约）。

        面向 workflow 节点的公开端口：``tool_observation_dispatcher`` 在观察分发阶段
        调用本方法把观察写回 ``RuntimeContextManager``，闭合 ``AIMessage.tool_calls``
        配对。内部委托 :meth:`_to_model_message`，序列化规则以其为准。

        参数:
            observation: 已治理的工具观察（含正常结果、错误占位、取消占位）。

        返回:
            可并入模型上下文的 ``ToolMessage``。

        异常:
            无（纯序列化）。

        副作用:
            无（不修改入参观察对象，不写上下文——写回由调用方执行）。
        """

        return self._to_model_message(observation)

    def _to_model_message(self, observation: ToolObservation) -> ToolMessage:
        """把工具观察压缩为模型可消费的 ``role="tool"`` 消息。

        三类终态使用不同的最小消息契约：

        - ``success``：只发送成功内容；没有内容时发送 ``"success"``。
        - ``error``：发送 ``error``、``retryable``、可选的重试判断提示和 ``reason``，
          避免重复发送 ``content``（失败观察的 ``content`` 通常就是 ``error`` 的副本）。
        - ``cancelled``：只发送取消说明，不把取消伪装成可重试错误。

        LangChain 当前 ``ToolMessage.status`` 只接受 ``"success"`` / ``"error"``，
        没有 ``"cancelled"``。取消的真实生命周期仍由 ``ToolObservation.status`` 和
        Transport 保存；写入模型上下文时将其映射为 ``status="error"``，并通过消息
        content 中的 ``cancelled`` 标记保留语义，避免构造 ``ToolMessage`` 时抛异常。

        参数:
            observation: 已产出的工具观察（含正常结果、错误占位、取消占位）。

        返回:
            可并入模型上下文的 ``ToolMessage``，其 ``tool_call_id`` 用于与
            ``AIMessage.tool_calls`` 配对闭合。

        异常:
            无。

        副作用:
            无（不修改入参观察对象）。
        """
        content = observation.content.strip() if isinstance(observation.content, str) else ""
        error = observation.error.strip() if isinstance(observation.error, str) else ""
        reason = observation.reason.strip() if isinstance(observation.reason, str) else ""

        if observation.status == "success":
            content_text = content or "success"
            message_status = "success"
        elif observation.status == "cancelled":
            cancellation_detail = reason
            content_text = (
                f"cancelled: {cancellation_detail}" if cancellation_detail else "cancelled"
            )
            # ToolMessage 没有 cancelled 状态；不能把非法值传给 LangChain。
            message_status = "error"
        else:
            error_detail = error or "the tool call failed"
            lines = [
                f"error: {error_detail}",
                f"retryable: {str(observation.retryable).lower()}",
            ]
            if observation.retryable:
                lines.append(
                    "hint: this error can be retried; decide from the context whether "
                    "retrying is appropriate."
                )
            if reason:
                lines.append(f"reason: {reason}")
            content_text = "\n".join(lines)
            message_status = "error"

        return ToolMessage(
            content=content_text, tool_call_id=observation.tool_call_id, status=message_status
        )
