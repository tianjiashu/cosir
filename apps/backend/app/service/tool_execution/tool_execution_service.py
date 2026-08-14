"""工具执行编排服务。

单一职责：编排一次模型请求的工具调用批次执行，并产出供下一步模型使用的观察结果与消息。
权限校验委托给 ``ToolScheduler``（其 ``execute`` 已按策略返回 ``permission_denied`` /
``unknown_tool`` / ``schema_invalid`` 等观察）。

职责边界：
- 负责：批量执行工具调用、发出工具生命周期事件、把观察结果转为模型消息。
- 不负责：工具注册、参数校验细节、子进程隔离（均由 ``ToolScheduler`` / ``ToolExecutor`` 负责）；
  也不负责任何渲染——事件只透传工具的静态展示声明与结构化数据，摘要与条目由客户端生成。
"""

import asyncio
import dataclasses
import json
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from app.config.logging.logger import log
from app.config.settings import Settings
from app.models import RuntimeMessage
from app.models.enums.error_kind import ErrorKind
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload import (
    ToolCallFinishedPayload,
    ToolCallStartedPayload,
    ToolOutputDeltaPayload,
)
from app.models.payload.file_change_updated_payload import FileChangeUpdatedPayload
from app.models.payload.runtime_event_payload import RuntimeEventPayload
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus
from app.service.tool_execution.run_result import ToolRunResult
from app.service.tool_execution.tool_trace_recorder import (
    ToolTraceRecorder,
    _NullToolTraceRecorder,
)
from app.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_error import (
    cancel_not_executed_reason,
    internal_execution_error_reason,
    tool_cancelled,
    tool_error,
)
from app.tools.tool_execute.tool_scheduler import ToolScheduler
from app.tools.tool_handler.patch.patch_diff import FileDiffResult, build_diff_stats
from app.tools.tool_handler.terminal import OutputSink
from app.utils.trace_infra.redaction import redact_terminal_output


class ToolExecutionService:
    """Orchestrate a batch of tool calls requested by the model."""

    def __init__(
        self,
        scheduler: ToolScheduler,
        agent_id: str,
        allowed_tool_names: Iterable[str] | None = None,
        tool_definitions: list[ToolDefinition] | None = None,
        trace_recorder: ToolTraceRecorder | None = None,
        should_cancel: Callable[[], bool] | None = None,
        event_bus: RuntimeEventBus | None = None,
    ) -> None:
        """Initialize the tool execution service.

        参数:
            scheduler: 底层工具调度器（负责校验与执行）。
            agent_id: 执行主体标识（用于日志关联）。
            allowed_tool_names: 当前 Agent profile 允许执行的工具名。
            tool_definitions: 本次运行暴露给模型的工具定义列表；用于按工具名取静态
                ``ToolDisplayHints`` 并随 ``TOOL_CALL_STARTED`` 透传给客户端。
                ``None`` 时事件不携带展示声明，客户端降级为通用展示。
            trace_recorder: 可选的工具调用 trace 记录器（依赖倒置，实现在 core/observability）。
                ``None`` 时退化为空实现（``_NullToolTraceRecorder``），不产生任何 trace 开销。
            should_cancel: 可选的运行时取消检查回调；返回 True 时停止执行后续工具。
            event_bus: 可选的运行时事件总线；提供时承载两条**不持久化**的实时广播通道：
                每次产生文件变更的工具调用完成后广播 ``FILE_CHANGE_UPDATED``；
                命令类工具运行期逐段广播 ``TOOL_OUTPUT_DELTA``。二者均只驱动前端实时展示。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self._scheduler = scheduler
        self._agent_id = agent_id
        self._allowed_tool_names = (
            frozenset(allowed_tool_names) if allowed_tool_names is not None else None
        )
        self._display_by_name = {
            definition.name: definition.display
            for definition in (tool_definitions or [])
            if definition.display is not None
        }
        self._parallel_mode_by_name = {
            definition.name: definition.parallel_mode for definition in (tool_definitions or [])
        }
        self._trace_recorder = trace_recorder or _NullToolTraceRecorder()
        self._should_cancel = should_cancel or (lambda: False)
        self._event_bus = event_bus

    def run_calls_with_events(
        self,
        step_id: str,
        calls: list[ToolCall],
        execution_context: ToolExecutionContext | None = None,
        write_event: Callable[[EventType, RuntimeEventPayload], None] | None = None,
        running_loop: asyncio.AbstractEventLoop | None = None,
    ) -> ToolRunResult:
        """执行一批工具调用并发出生命周期事件。

        每个调用经 ``ToolScheduler.execute`` 执行（其内部完成权限与参数校验），观察结果
        转为 ``role="tool"`` 的 ``RuntimeMessage`` 供下一步模型消费；若提供 ``write_event``
        回调，则对每个完成的工具调用发出 ``TOOL_CALL_FINISHED`` 事件。

        入口按工具声明的调度模式（``ToolDefinition.parallel_mode``）分流：串行调用留在
        本方法的串行路径逐个执行；声明为 ``parallel`` 的调用统一交给
        ``_run_calls_with_parallel_modes`` 并发执行（该方法只处理并行组，不再混入串行
        分支）。两条路径都保留原始 index，最终由 ``_build_result_with_cancel_placeholders``
        按原始顺序合并、补占位并统一序列化为模型消息。

        配对闭合不变量（本方法收口）：``AIMessage.tool_calls`` 的每个 call 必须在返回的
        模型消息中配对一条 ``role="tool"`` 消息，否则下一轮对话会因协议不匹配崩溃。为此，
        两类失败来源都会被收口为 ``status="error"`` 的占位观察并序列化进模型消息：
        （1）协作式取消——在 call 边界检测到取消信号后未执行的 call；
        （2）执行链内部 bug——``ToolScheduler.execute`` / trace span / 事件构造 / 序列化
        抛出的非工具语义异常（此时工具本体未运行）。

        参数:
            step_id: 请求这些工具调用的步骤标识。
            calls: 模型请求的工具调用列表。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                透传给 ``ToolScheduler.execute``，最终在执行期注入 handler。
            write_event: 可选的工具生命周期事件写入回调。
            running_loop: 承载本轮运行的事件循环。本方法通常被异步节点经
                ``asyncio.to_thread`` 调度到工作线程执行，无法自行获取该循环，
                故由调用方传入，用于把命令运行期输出增量广播调度回循环线程。
                缺省时禁用实时输出通道，其余行为不变。

        返回:
            含观察列表与模型消息的 ``ToolRunResult``。观察与消息数量恒等于 ``calls``
            数量，且按入参原始顺序返回——未执行的 call（取消跳过）在其原始 index
            位置以取消占位补齐，保证每个 call_id 的 ``tool_calls`` 协议配对闭合。

        异常:
            当 ``write_event`` 为 ``None`` 时抛出 ``RuntimeError``（调用方必须提供事件
            写入回调，否则无法发出生命周期事件）。单个工具失败或执行链内部异常均**不**
            向上抛出，而是由观察结果的 ``status`` 表达。

        副作用:
            可能通过 ``write_event`` 写入 ``TOOL_CALL_STARTED`` / ``TOOL_CALL_FINISHED``
            事件；执行链内部异常时写 error 日志（含堆栈），本批因取消跳过调用时写 warning
            日志；可能经 ``_publish_file_change_updated`` 广播运行中文件变更。
        """

        if write_event is None:
            raise RuntimeError("write_event is None")

        # 本方法整体运行在 asyncio.to_thread 的工作线程上，无法用 get_running_loop 取到
        # 承载本轮的事件循环，故由调用方（异步节点）显式传入，供实时输出通道调度回环。
        loop = running_loop

        # 入口分流：按工具声明的调度模式，把本批调用拆成「串行组」与「并行组」。
        # 串行组保留原始相对顺序逐个执行；并行组统一交给 _run_calls_with_parallel_modes
        # 并发执行（该方法只处理并行调用，不再混入串行分支）。两组都保留原始 index，
        # 最终由 _build_result_with_cancel_placeholders 按原始顺序合并、补占位并统一
        # 序列化为模型消息（配对闭合单一收口）。
        serial_calls: list[tuple[int, ToolCall]] = []
        parallel_calls: list[tuple[int, ToolCall]] = []
        for index, call in enumerate(calls):
            if self._is_parallel_call(call):
                parallel_calls.append((index, call))
            else:
                serial_calls.append((index, call))

        indexed_observations: list[tuple[int, ToolObservation]] = []

        # 串行组：逐个执行，取消检查只发生在 call 边界（协作式取消）。
        # 执行与事件收口复用并行路径同一套辅助方法（_emit_tool_call_started /
        # _execute_tool_call / _handle_completed_observation），串行/并行行为一致，
        # 避免平行复制事件载荷、日志格式与广播守卫造成语义漂移。
        for index, call in serial_calls:
            if self._should_cancel():
                break
            self._emit_tool_call_started(step_id, call, write_event)
            observation = self._execute_tool_call(step_id, call, execution_context, loop)
            indexed_observations.append((index, observation))
            self._handle_completed_observation(
                step_id, observation, execution_context, write_event, loop
            )

        # 并行组：统一交给并行执行器（该方法只处理并行调用）。取消已生效时并行组
        # 整体跳过，未执行的 call 由 _build_result_with_cancel_placeholders 补占位。
        if parallel_calls and not self._should_cancel():
            indexed_observations.extend(
                self._run_calls_with_parallel_modes(
                    step_id=step_id,
                    calls=parallel_calls,
                    execution_context=execution_context,
                    write_event=write_event,
                    loop=loop,
                )
            )

        # 配对闭合不变量收口：按原始 index 合并排序、为未执行的 call 补取消占位，
        # 并统一序列化为 role="tool" 模型消息（单一收口，避免平行复制语义漂移）。
        return self._build_result_with_cancel_placeholders(step_id, calls, indexed_observations)

    def _run_calls_with_parallel_modes(
        self,
        step_id: str,
        calls: list[tuple[int, ToolCall]],
        execution_context: ToolExecutionContext | None,
        write_event: Callable[[EventType, RuntimeEventPayload], None],
        loop: asyncio.AbstractEventLoop | None,
    ) -> list[tuple[int, ToolObservation]]:
        """并发执行一批已声明为可并行调度的工具调用。

        只处理并行组：``run_calls_with_events`` 入口已按工具声明的调度模式完成分流，
        本方法不再包含串行分支；串行调用始终留在串行路径逐个执行。

        参数:
            step_id: 请求这些工具调用的步骤标识。
            calls: 带原始位置的并行工具调用列表（元组 ``(index, call)``）。
            execution_context: 本次工具执行上下文。
            write_event: 工具生命周期事件写入回调。
            loop: 承载实时事件广播的 asyncio 事件循环。

        返回:
            带原始位置的已执行观察列表（按实际完成顺序）。批次中途取消时可能少于
            传入数量；未执行部分由调用方经 ``_build_result_with_cancel_placeholders``
            在原始 index 位置补取消占位。

        异常:
            无。worker 抛出的意外异常会被收口为对应 call 的 error 观察。

        副作用:
            启动临时线程池执行工具，并写入 started/finished 生命周期事件。
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
                """提交待执行调用，直到达到 worker 上限或检测到取消。

                参数:
                    无。

                返回:
                    无。

                异常:
                    透传事件写入或线程池提交异常，由外层执行链路收口。

                副作用:
                    写入 started 事件，并向线程池提交新的工具调用。
                """
                while (
                    pending_calls
                    and len(future_by_call) < max_workers
                    and not self._should_cancel()
                ):
                    batch_index, batch_call = pending_calls.pop()
                    self._emit_tool_call_started(step_id, batch_call, write_event)
                    future_by_call[
                        pool.submit(
                            self._execute_tool_call,
                            step_id,
                            batch_call,
                            execution_context,
                            loop,
                        )
                    ] = (batch_index, batch_call)

            _submit_until_full()
            while future_by_call:
                done_futures, _ = wait(future_by_call, return_when=FIRST_COMPLETED)
                for future in done_futures:
                    index, call = future_by_call.pop(future)
                    try:
                        observation = future.result()
                    except Exception as exc:
                        observation = self._internal_error_observation(step_id, call, exc)
                    self._handle_completed_observation(
                        step_id, observation, execution_context, write_event, loop
                    )
                    completed.append((index, observation))
                _submit_until_full()
        return completed

    def _execute_tool_call(
        self,
        step_id: str,
        call: ToolCall,
        execution_context: ToolExecutionContext | None,
        loop: asyncio.AbstractEventLoop | None,
    ) -> ToolObservation:
        """执行单个工具调用，并把执行链路异常收口为工具观察。

        参数:
            step_id: 请求该工具调用的步骤标识。
            call: 当前工具调用。
            execution_context: 本次工具执行上下文。
            loop: 承载实时事件广播的 asyncio 事件循环。

        返回:
            调度器返回的观察，或内部异常对应的 error 观察。

        异常:
            无。内部异常在本方法内转为 ``ToolObservation``。

        副作用:
            调用底层 ``ToolScheduler``，并记录可选 trace span。
        """
        try:
            with self._trace_recorder.span(call, step_id) as tool_span:
                observation = self._scheduler.execute(
                    call,
                    execution_context=execution_context,
                    allowed_tool_names=self._allowed_tool_names,
                    should_cancel=self._should_cancel,
                    output_sink=self._build_output_sink(step_id, call, execution_context, loop),
                )
                tool_span.record(observation)
                return observation
        except Exception as exc:
            return self._internal_error_observation(step_id, call, exc)

    def _internal_error_observation(
        self,
        step_id: str,
        call: ToolCall,
        exc: Exception,
    ) -> ToolObservation:
        """把工具执行链路内部异常转换为稳定的 error 观察。

        参数:
            step_id: 请求该工具调用的步骤标识。
            call: 当前工具调用。
            exc: 被捕获的执行链路异常。

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

    def _emit_tool_call_started(
        self,
        step_id: str,
        call: ToolCall,
        write_event: Callable[[EventType, RuntimeEventPayload], None],
    ) -> None:
        """写入工具调用开始事件。

        参数:
            step_id: 请求该工具调用的步骤标识。
            call: 当前工具调用。
            write_event: 工具生命周期事件写入回调。

        返回:
            无。

        异常:
            无。``write_event`` 异常由 ``_emit_event_safely`` 收口，不向上抛出。

        副作用:
            写入一条 ``TOOL_CALL_STARTED`` 事件；写入失败时记 error 日志。
        """
        display: ToolDisplayHints | None = self._display_by_name.get(call.tool_name)
        display_payload = dataclasses.asdict(display) if display is not None else None
        self._emit_event_safely(
            step_id,
            call.call_id,
            call.tool_name,
            write_event,
            EventType.TOOL_CALL_STARTED,
            ToolCallStartedPayload(
                tool_name=call.tool_name,
                step_id=step_id,
                tool_call_id=call.call_id,
                arguments=call.arguments,
                display=display_payload,
            ),
        )

    def _handle_completed_observation(
        self,
        step_id: str,
        observation: ToolObservation,
        execution_context: ToolExecutionContext | None,
        write_event: Callable[[EventType, RuntimeEventPayload], None],
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        """处理单个工具观察的完成侧效应。

        参数:
            step_id: 请求该工具调用的步骤标识。
            observation: 工具执行结果观察。
            execution_context: 本次工具执行上下文。
            write_event: 工具生命周期事件写入回调。
            loop: 承载实时事件广播的 asyncio 事件循环。

        返回:
            无。

        异常:
            无。``write_event`` 异常由 ``_emit_event_safely`` 收口，不向上抛出。

        副作用:
            写入 ``TOOL_CALL_FINISHED``（其 ``status`` 透传观察的终态：
            ``"success"``/``"cancelled"`` 原样透传，其余一律 ``"error"``，使取消态
            不再被塌缩成失败），并在成功产生文件变更时广播实时文件变更事件。
        """
        # 透传成功/取消终态，其余（含未知状态）归一为 error，避免取消态在前端被误读为失败。
        event_status = (
            observation.status if observation.status in ("success", "cancelled") else "error"
        )
        self._emit_event_safely(
            step_id,
            observation.tool_call_id,
            observation.tool_name,
            write_event,
            EventType.TOOL_CALL_FINISHED,
            ToolCallFinishedPayload(
                step_id=step_id,
                tool_name=observation.tool_name,
                status=event_status,
                tool_call_id=observation.tool_call_id,
                content=observation.content,
                error=observation.error,
                reason=observation.reason,
                retryable=observation.retryable,
                data=observation.data or {},
            ),
        )
        if (
            self._event_bus is not None
            and observation.status == "success"
            and execution_context is not None
            and execution_context.task_id
            and execution_context.turn_id
        ):
            self._publish_file_change_updated(execution_context, observation, loop)

    def _emit_event_safely(
        self,
        step_id: str,
        tool_call_id: str,
        tool_name: str,
        write_event: Callable[[EventType, RuntimeEventPayload], None],
        event_type: EventType,
        payload: RuntimeEventPayload,
    ) -> None:
        """安全写入工具生命周期事件，事件通道故障不中断工具执行。

        参数:
            step_id: 请求这些工具调用的步骤标识。
            tool_call_id: 当前工具调用的协议配对 id（仅用于日志上下文）。
            tool_name: 当前工具名（仅用于日志上下文）。
            write_event: 工具生命周期事件写入回调。
            event_type: 事件类型。
            payload: 事件载荷。

        返回:
            无。

        异常:
            无。``write_event`` 抛出的异常（SSE 断连、载荷序列化失败等）被记入
            error 日志后吞掉——事件通道故障不应破坏工具执行结果与协议配对闭合。

        副作用:
            ``write_event`` 成功时写入一条生命周期事件；失败时写 error 日志（含堆栈）。
        """
        try:
            write_event(event_type, payload)
        except Exception as exc:
            log.exception(
                "tool_event_write_failed",
                extra={
                    "msg": "工具生命周期事件写入失败，已忽略以保持执行不中断",
                    "data": {
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "step_id": step_id,
                        "event_type": event_type.value,
                        "error": type(exc).__name__,
                    },
                },
                exc_info=True,
            )

    def _build_result_with_cancel_placeholders(
        self,
        step_id: str,
        calls: list[ToolCall],
        indexed_observations: list[tuple[int, ToolObservation]],
    ) -> ToolRunResult:
        """补齐取消占位并构建返回给模型的工具结果。

        参数:
            step_id: 请求这些工具调用的步骤标识。
            calls: 模型请求的工具调用列表。
            indexed_observations: 已产生观察的原始位置与观察列表。

        返回:
            按原始 call 顺序排列且协议闭合的 ``ToolRunResult``。

        异常:
            无。

        副作用:
            当存在跳过调用时写 warning 日志；序列化模型消息时会清空 display_data。
        """
        executed_indices = {index for index, _ in indexed_observations}
        skipped_calls = [
            (index, call)
            for index, call in enumerate(calls)
            if index not in executed_indices
        ]
        if skipped_calls:
            log.warning(
                "tool_calls_cancelled_not_executed",
                extra={
                    "msg": "本批工具调用因取消未执行，已补 cancelled 占位闭合协议",
                    "data": {
                        "step_id": step_id,
                        "total": len(calls),
                        "executed": len(executed_indices),
                        "skipped_call_ids": [call.call_id for _, call in skipped_calls],
                    },
                },
            )
            for index, call in skipped_calls:
                indexed_observations.append(
                    (
                        index,
                        tool_cancelled(
                            tool_name=call.tool_name,
                            reason=cancel_not_executed_reason(),
                            error="the tool call was cancelled before execution",
                            tool_call_id=call.call_id,
                        ),
                    )
                )
        indexed_observations.sort(key=lambda item: item[0])
        observations = [observation for _, observation in indexed_observations]
        messages = [self._to_model_message(observation) for observation in observations]
        return ToolRunResult(observations=observations, messages_for_model=messages)

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

    def _to_model_message(self, observation: ToolObservation) -> RuntimeMessage:
        """把单个工具观察序列化为模型可见的 ``role="tool"`` 消息。

        统一收口所有观察（含正常结果、内部错误占位、取消占位）的序列化逻辑，避免主路径
        与补占位分支平行复制导致的语义漂移。序列化前清空 ``display_data``（模型不可见
        通道），并对 ``content`` 做终端输出脱敏；最终仅保留非空字段，确保面向模型的文本
        与正常失败观察同构。

        参数:
            observation: 已产出的工具观察（任意来源，含占位）。

        返回:
            可并入模型上下文的 ``RuntimeMessage``，``metadata.tool_call_id`` 用于与
            ``AIMessage.tool_calls`` 配对闭合。

        异常:
            无。

        副作用:
            调用 ``observation.clear_display_data()`` 清空原观察对象的展示数据（就地修改
            入参，不另存）。
        """
        observation.clear_display_data()
        serialized = dataclasses.asdict(observation)
        serialized["content"] = redact_terminal_output(observation.content)
        return RuntimeMessage(
            role="tool",
            content_text=json.dumps(
                {k: v for k, v in serialized.items() if v is not None},
                ensure_ascii=False,
            ),
            metadata={"tool_call_id": observation.tool_call_id},
        )

    def _build_output_sink(
        self,
        step_id: str,
        call: ToolCall,
        execution_context: ToolExecutionContext | None,
        loop: asyncio.AbstractEventLoop | None,
    ) -> OutputSink | None:
        """构造把命令运行期输出片段广播为 ``TOOL_OUTPUT_DELTA`` 的回调。

        与 ``FILE_CHANGE_UPDATED`` 同属「运行中实时广播」通道：经 ``RuntimeEventBus``
        发布且**不持久化**到 ``runtime_events``（终态完整输出已由 ``TOOL_CALL_FINISHED``
        承载，逐行落库会放大写入量且回放时与终态输出重复）。

        返回的回调运行在 ``ToolExecutor`` 等待子进程结果的**工作线程**上，而事件总线的
        订阅者投递需在事件循环线程执行，故经 ``loop.call_soon_threadsafe`` 调度回环。
        缺少总线、事件循环或 task/turn 上下文时返回 ``None``——由调用方据此跳过实时通道，
        不构造无处可发的回调。

        参数:
            step_id: 产生该工具调用的步骤标识。
            call: 当前工具调用，取其 ``call_id`` 作为前端归并键。
            execution_context: 本次执行的运行时边界；需含 ``task_id`` / ``turn_id``。
            loop: 承载本次运行的事件循环；用于把广播动作调度回循环线程。

        返回:
            ``OutputSink`` 回调（签名 ``(text, truncated) -> None``）；
            实时通道不可用时返回 ``None``。

        异常:
            返回的回调不向上抛出：调度失败只记 warning，避免实时展示故障反压命令执行
            （``ToolExecutor`` 侧亦会因异常关闭实时通道）。

        副作用:
            调用时向事件循环投递一次广播，经 ``RuntimeEventBus`` 发出一条不持久化的
            ``TOOL_OUTPUT_DELTA`` 事件。
        """

        bus = self._event_bus
        if (
            bus is None
            or loop is None
            or execution_context is None
            or not execution_context.task_id
            or not execution_context.turn_id
        ):
            return None

        task_id = execution_context.task_id
        turn_id = execution_context.turn_id

        def _sink(text: str, truncated: bool) -> None:
            event = RuntimeEvent(
                event_type=EventType.TOOL_OUTPUT_DELTA,
                task_id=task_id,
                turn_id=turn_id,
                payload=ToolOutputDeltaPayload(
                    tool_call_id=call.call_id,
                    step_id=step_id,
                    text=text,
                    truncated=truncated,
                ),
            )
            try:
                loop.call_soon_threadsafe(bus.publish, event)
            except RuntimeError:
                # 事件循环已关闭（turn 提前结束/取消）：实时展示降级，命令继续跑完。
                log.warning(
                    "tool_output_delta_publish_failed",
                    extra={
                        "msg": "工具输出增量广播失败，实时展示降级，不影响工具执行",
                        "data": {
                            "tool_name": call.tool_name,
                            "tool_call_id": call.call_id,
                            "step_id": step_id,
                            "turn_id": turn_id,
                        },
                    },
                )

        return _sink

    def _publish_file_change_updated(
        self,
        execution_context: ToolExecutionContext,
        observation: ToolObservation,
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        """广播本次工具调用产生的文件变更，驱动前端运行中实时展示。

        本方法运行在 ``asyncio.to_thread`` 的工作线程上（``run_calls_with_events``
        整体被异步节点调度到线程池），而事件总线的订阅者投递需在事件循环线程执行，
        故与 ``TOOL_OUTPUT_DELTA`` 采用同一范式，经 ``loop.call_soon_threadsafe``
        调度回环，不在工作线程直接操作订阅队列。

        参数:
            execution_context: 本次执行的运行时边界（含 task_id / turn_id）。
            observation: 工具观察结果；其 ``data["changes"]`` 为单文件变更字典列表，
                每个含 ``path`` / ``before`` / ``after`` 与变更动作
                （``action`` 或 ``status`` 字段）。
            loop: 承载本轮运行的事件循环；为 ``None`` 时跳过广播（降级为仅全量查询可见）。

        返回:
            无。

        异常:
            无。广播属展示侧增强，失败不应影响工具执行主流程，故整体捕获并记 warning。

        副作用:
            经注入的 ``RuntimeEventBus`` 发布若干条不持久化的 ``FILE_CHANGE_UPDATED`` 事件，
            每条携带该文件实时 diff（additions / deletions / before / after），供前端零延迟渲染。
        """
        bus = self._event_bus
        if bus is None or loop is None:
            return
        changes = (observation.data or {}).get("changes")
        if not isinstance(changes, list) or not changes:
            return
        try:
            # 复用既有 diff 统计能力计算每文件增删行数，避免重复实现差异算法。
            # 与采集层 FileSnapshotHook._change_diff_stats 保持同一语义
            # （status 映射由 build_diff_stats 处理）。
            # 顺序对齐：build_diff_stats 的 files[] 与输入一一对应（不按 path 去重），
            # 故用「统一过滤后的列表」同时驱动统计与循环，既容忍非 dict 混入，
            # 也保证同一 path 多次变更时各自取到自己的统计（path 字典会覆盖错配）。
            valid_changes = [change for change in changes if isinstance(change, dict)]
            diff_results = [
                FileDiffResult(
                    path=str(change.get("path", "")),
                    status=str(change.get("status") or "modified"),
                    before=str(change.get("before") or ""),
                    after=str(change.get("after") or ""),
                )
                for change in valid_changes
            ]
            stats = build_diff_stats(diff_results)
            file_stats = stats.get("files", [])
            for change, file_stat in zip(valid_changes, file_stats, strict=True):
                path = change.get("path")
                action = change.get("action") or change.get("status")
                if not path or not action:
                    continue
                event = RuntimeEvent(
                    event_type=EventType.FILE_CHANGE_UPDATED,
                    task_id=execution_context.task_id,
                    turn_id=execution_context.turn_id,
                    payload=FileChangeUpdatedPayload(
                        task_id=execution_context.task_id,
                        turn_id=execution_context.turn_id,
                        path=str(path),
                        action=str(action),
                        additions=int(file_stat.get("insertions") or 0),
                        deletions=int(file_stat.get("deletions") or 0),
                        before=change.get("before"),
                        after=change.get("after"),
                    ),
                )
                loop.call_soon_threadsafe(bus.publish, event)
        except Exception:
            log.exception(
                "file_change_updated_publish_failed",
                extra={
                    "msg": "运行中文件变更实时广播失败，不影响工具执行",
                    "data": {
                        "task_id": execution_context.task_id,
                        "turn_id": execution_context.turn_id,
                    },
                },
            )
