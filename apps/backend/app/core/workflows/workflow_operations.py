"""暴露给工作流策略的运行时操作门面。

本模块只承载「工作流可用的运行期副作用」这一层边界：工作流节点经 ``WorkflowOperations``
使用模型、工具与状态能力，不直接持有 CRUD、service 或执行器。门面的状态事实来源是当前绑定的
Conversation Run；run 状态迁移与事件发布委托 ``ConversationRunStateService``，Transport 投影
委托 conversation event projector。

不负责：工作流路由（见 ``react`` 包）、工具实现（见 ``core/tools``）、Run 状态机本身
（见 ``service/task``）。
"""

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
from app.core.runtime.conversation_run_cancellation_registry import (
    cancellation_registry,
)
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
    get_conversation_run_state_service,
)

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile


class WorkflowOperations:
    """以窄接口向工作流暴露运行时拥有的副作用。

    状态单一事实来源是 ``ConversationRun``：本门面暴露的状态查询与状态迁移方法都作用于 run，
    不写 task 执行态（task 执行态由最新 run 派生）。
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
            取消判定），存储执行上下文与 canonical writer，记初始化日志。
        """

        self._conversation_run_state_service = get_conversation_run_state_service()
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
        """返回本门面绑定的 Conversation Run 记录。

        返回:
            构造门面时注入的 ``ConversationRunRecord``。

        异常:
            无。

        副作用:
            无。
        """

        return self._current_run

    def get_current_task(self) -> TaskRecord:
        """返回本门面绑定的任务记录。

        返回:
            构造门面时注入的 ``TaskRecord``。

        异常:
            无。

        副作用:
            无。
        """

        return self._current_task

    def get_current_workspace(self) -> WorkspaceRecord:
        """返回本门面绑定的工作区记录。

        返回:
            构造门面时注入的 ``WorkspaceRecord``。

        异常:
            无。

        副作用:
            无。
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
        """返回当前绑定的 run 是否已被要求停止。

        取消信号来自进程内 run 级取消注册表（set 语义，``cancellation_registry``）：执行上下文
        注入的 ``ToolRuntimeDependencies.is_run_cancelled`` 优先（它已冻结 run_id、直查同一
        注册表），未注入时本方法按 run id 兜底查询。

        本方法刻意不做 DB 兜底查询：工具取消检查会在 process 模式工具的 50ms 轮询中被反复调用，
        高频读库不可接受。信号在 run 执行结束时由 ``ConversationRunRunner`` 清空；注册表属当前
        后端进程内状态，不跨重启。

        参数:
            无。

        返回:
            当前 run 已被取消时返回 True；未绑定 run 或无取消信号时返回 False。

        异常:
            无。

        副作用:
            无（只读进程内信号）。
        """
        current_run = self._current_run
        if current_run is None:
            return False
        should_cancel = None
        if self._execution_context is not None:
            should_cancel = self._execution_context.runtime_dependencies.is_run_cancelled
        if should_cancel is not None:
            return should_cancel(current_run.id)
        return cancellation_registry.is_cancelled(current_run.id)

    def complete_run_if_running(
        self,
        usage_stats: ConversationRunUsageStats | None = None,
        final_output: str | None = None,
    ) -> ConversationRunRecord | None:
        """仅当 run 仍处于 running 时把它落定为 completed。

        参数:
            usage_stats: 可选的运行用量统计，透传给 ``ConversationRunStateService`` 以发布
                带用量的状态事件。
            final_output: 可选，Agent 对该轮次的最终回答文本，随终态一并写入 run 行。

        返回:
            成功完成时返回更新后的 ConversationRunRecord；run 已被取消/失败/完成时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时把 run 更新为 ``completed``（写入 ``final_output`` 与用量列）；状态变更
            事件已由 ``ConversationRunStateService`` 在条件更新命中后统一发布，本方法不直接发事件。
        """
        run_id = self._current_run.id
        log.info(
            "run_completion_attempted",
            extra={
                "msg": f"尝试完成 running run，run_id={run_id}",
                "data": {"run_id": run_id},
            },
        )
        record = self._conversation_run_state_service.complete_run_if_running(
            run_id, final_output=final_output, usage_stats=usage_stats
        )
        return record

    def fail_run_if_running(
        self,
        end_reason: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
        final_output: str | None = None,
    ) -> ConversationRunRecord | None:
        """仅当 run 仍处于 running 时把它落定为 failed。

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
            条件满足时把 run 更新为 ``failed``（写入 ``end_reason`` / ``final_output`` / 用量与
            受控错误契约）；状态变更事件由 ``ConversationRunStateService`` 在条件更新命中后发布，
            本方法不直接发事件。
        """
        run_id = self._current_run.id
        log.info(
            "run_failure_attempted",
            extra={
                "msg": f"尝试将 running run 标记为 failed，run_id={run_id}",
                "data": {"run_id": run_id, "end_reason": end_reason},
            },
        )
        record = self._conversation_run_state_service.fail_run_if_running(
            run_id, end_reason, final_output=final_output, usage_stats=usage_stats
        )
        return record

    def cancel_run_if_running(
        self,
        end_reason: str = "runtime_cancelled",
        usage_stats: ConversationRunUsageStats | None = None,
        final_output: str | None = None,
    ) -> ConversationRunRecord | None:
        """经 canonical writer 把仍处于 active 的 run 落定为 cancelled。

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
            条件满足时把 ``pending`` / ``running`` 的 run 更新为 ``cancelled``（写入
            ``end_reason`` / ``final_output`` / 用量与受控错误契约）；状态变更事件由
            ``ConversationRunStateService`` 在条件更新命中后发布，本方法不直接发事件。
        """
        run_id = self._current_run.id
        log.info(
            "run_cancel_attempted",
            extra={
                "msg": f"尝试将 running run 标记为 cancelled，run_id={run_id}",
                "data": {"run_id": run_id, "end_reason": end_reason},
            },
        )
        record = self._conversation_run_state_service.cancel_run_if_running(
            run_id, end_reason, final_output=final_output, usage_stats=usage_stats
        )
        return record

    async def run_tool_calls(
        self,
        task_id: int,
        calls: list[ToolCall],
        step_id: str | None = None,
        running_loop: asyncio.AbstractEventLoop | None = None,
    ) -> ToolRunResult:
        """经工具系统执行模型请求的工具调用。

        工具生命周期通过明确的 callback 写入 canonical conversation facts。门面持有的
        ``execution_context`` 在内部透传给执行链，最终在执行期注入各 handler。

        执行线程：串行工具调用经 ``asyncio.to_thread`` 移出事件循环线程（避免同步阻塞
        的 thread 模式 handler 占死 loop，连带卡死 SSE 与并发请求）；``to_thread`` 内部以
        ``copy_context().run`` 提交，保留父 turn 的 OTel/trace 上下文，语义与并行分支的
        ``ThreadPoolExecutor`` 提交一致。并行工具调用组仍由 ``_run_calls_with_parallel_modes``
        的线程池执行。

        参数:
            task_id: 当前任务标识符。
            calls: 模型请求的工具调用列表。
            step_id: 请求这些工具调用的步骤标识符。
            running_loop: 兼容历史签名的保留参数，**当前实现不使用**（工具执行期输出桥接已由
                执行层 handler 自行处理）。

        返回:
            ``ToolRunResult``；其 ``observations`` 由「串行组按传入顺序」与「并行组按实际完成
            顺序」拼接而成（未按原始下标重排）。

        异常:
            无。单个工具的执行链路异常在 ``_execute_tool_call`` 内收口为 error 观察。

        副作用:
            实际执行工具（文件、终端、搜索、委派等）；串行调用逐个 ``await``，并行调用进入临时
            线程池；状态写入 **run**。
        """

        serial_calls: list[tuple[int, ToolCall]] = []
        parallel_calls: list[tuple[int, ToolCall]] = []
        for index, call in enumerate(calls):
            if self._is_parallel_call(call):
                parallel_calls.append((index, call))
            else:
                serial_calls.append((index, call))

        indexed_observations: list[tuple[int, ToolObservation]] = []
        # 串行工具调用经 asyncio.to_thread 移出事件循环线程：thread 模式工具同步阻塞
        # （如大仓库 search_files、重 CPU/IO handler）会连带卡死 SSE 推送与并发请求。
        # to_thread 内部以 copy_context().run 提交，保留父 turn 的 OTel/trace 上下文，
        # 与并行分支 copy_context().run 语义一致；逐调用 await 让取消可在工具间被观察到。
        for index, call in serial_calls:
            observation = await asyncio.to_thread(self._execute_tool_call, task_id, call, step_id)
            indexed_observations.append((index, observation))

        if parallel_calls:
            indexed_observations.extend(
                self._run_calls_with_parallel_modes(task_id, parallel_calls, step_id)
            )

        executed_observations = [observation for _, observation in indexed_observations]
        return ToolRunResult(observations=executed_observations)

    def _run_calls_with_parallel_modes(
        self,
        task_id: int,
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
            带原始位置的已执行观察列表，顺序为**实际完成顺序**（未按原始下标重排）；每个传入
            调用都会产出条观察。

        异常:
            无。worker 抛出的意外异常会被收口为对应 call 的 error 观察。

        副作用:
            启动临时线程池执行工具；每次提交前复制一份 contextvars 快照，不引入跨线程可变状态。
        """
        if not calls:
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
                """提交待执行调用，直到在途任务数达到 worker 上限或没有待提交调用。

                副作用:
                    向线程池提交任务并登记 ``future -> (index, call)`` 映射；不改动工具调用本身。
                """
                while (
                    pending_calls
                    and len(future_by_call) < max_workers
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
        task_id: int,
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
                )
                tool_span.record(observation)
                return observation
        except Exception as exc:
            return self._internal_error_observation(task_id, call, exc, step_id)

    def _internal_error_observation(
        self,
        task_id: int,
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

        面向 workflow 节点的公开端口：``observe`` 节点在观察分发阶段（经
        ``ToolCallLifecycleManager.settle_batch``）调用本方法把观察写回
        ``RuntimeContextManager``，闭合 ``AIMessage.tool_calls`` 配对。内部委托
        :meth:`_to_model_message`，序列化规则以其为准。

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
        - ``error``：发送 ``error``、``retryable``、重试判断提示和 ``reason``，避免重复
          发送 ``content``（失败观察的 ``content`` 通常就是 ``error`` 的副本）。
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
            else:
                lines.append("hint: do not retry this tool call.")
            if reason:
                lines.append(f"reason: {reason}")
            content_text = "\n".join(lines)
            message_status = "error"

        return ToolMessage(
            content=content_text, tool_call_id=observation.tool_call_id, status=message_status
        )
