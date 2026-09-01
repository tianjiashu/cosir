"""工具执行编排服务。

单一职责：编排一次模型请求的工具调用批次执行，并产出供下一步模型使用的观察结果与消息。
权限校验委托给 ``ToolScheduler``（其 ``execute`` 已按策略返回 ``permission_denied`` /
``unknown_tool`` / ``schema_invalid`` 等观察）。

职责边界：
- 负责：批量执行工具调用、通知工具生命周期回调、把观察结果转为模型消息。
- 不负责：工具注册、参数校验细节、子进程隔离（均由 ``ToolScheduler`` / ``ToolExecutor`` 负责）；
  也不负责任何渲染。
"""

import asyncio
import contextvars
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import cast

from app.config.logging.logger import log
from app.config.settings import Settings
from app.models import RuntimeMessage
from app.models.enums.error_kind import ErrorKind
from app.service.tool_execution.run_result import ToolRunResult
from app.service.tool_execution.tool_trace_recorder import (
    ToolTraceRecorder,
    _NullToolTraceRecorder,
)
from app.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_cancelled import (
    CANCEL_NOT_EXECUTED_REASON,
    tool_cancelled,
)
from app.tools.tool_execute.tool_error import (
    internal_execution_error_reason,
    tool_error,
)
from app.tools.tool_execute.tool_scheduler import ToolScheduler

ToolCallStartedCallback = Callable[[str, ToolCall], None]
ToolCallFinishedCallback = Callable[[str, ToolObservation], None]


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
    ) -> None:
        """Initialize the tool execution service.

        参数:
            scheduler: 底层工具调度器（负责校验与执行）。
            agent_id: 执行主体标识（用于日志关联）。
            allowed_tool_names: 当前 Agent profile 允许执行的工具名。
            tool_definitions: 本次运行暴露给模型的工具定义列表；用于按工具名取静态
                读取工具的调度模式；``None`` 时不额外声明调度模式。
            trace_recorder: 可选的工具调用 trace 记录器（依赖倒置，实现在 core/observability）。
                ``None`` 时退化为空实现（``_NullToolTraceRecorder``），不产生任何 trace 开销。
            should_cancel: 可选的运行时取消检查回调；返回 True 时停止执行后续工具。

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
        self._parallel_mode_by_name = {
            definition.name: definition.parallel_mode for definition in (tool_definitions or [])
        }
        self._trace_recorder = trace_recorder or _NullToolTraceRecorder()
        self._should_cancel = should_cancel or (lambda: False)

    def run_calls(
        self,
        step_id: str,
        calls: list[ToolCall],
        execution_context: ToolExecutionContext | None = None,
        on_tool_call_started: ToolCallStartedCallback | None = None,
        on_tool_call_finished: ToolCallFinishedCallback | None = None,
        running_loop: asyncio.AbstractEventLoop | None = None,
    ) -> ToolRunResult:
        """执行一批工具调用并通知明确的工具生命周期回调。

        每个调用经 ``ToolScheduler.execute`` 执行（其内部完成权限与参数校验），观察结果
        转为 ``role="tool"`` 的 ``RuntimeMessage`` 供下一步模型消费。开始与完成回调只
        传递工具执行事实，不依赖通用运行时事件信封。

        入口按工具声明的调度模式（``ToolDefinition.parallel_mode``）分流：串行调用留在
        本方法的串行路径逐个执行；声明为 ``parallel`` 的调用统一交给
        ``_run_calls_with_parallel_modes`` 并发执行（该方法只处理并行组，不再混入串行
        分支）。两条路径都保留原始 index，最终由 ``_build_result_with_cancel_placeholders``
        按原始顺序合并、补占位并统一序列化为模型消息。

        配对闭合不变量（本方法收口）：``AIMessage.tool_calls`` 的每个 call 必须在返回的
        模型消息中配对一条 ``role="tool"`` 消息，否则下一轮对话会因协议不匹配崩溃。为此，
        两类失败来源都会被收口为 ``status="error"`` 的占位观察并序列化进模型消息：
        （1）协作式取消——在 call 边界检测到取消信号后未执行的 call；
        （2）执行链内部 bug——``ToolScheduler.execute`` / trace span / 回调前的序列化
        抛出的非工具语义异常（此时工具本体未运行）。

        参数:
            step_id: 请求这些工具调用的步骤标识。
            calls: 模型请求的工具调用列表。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                透传给 ``ToolScheduler.execute``，最终在执行期注入 handler。
            on_tool_call_started: 工具调用开始时调用，异常直接向上传播。
            on_tool_call_finished: 工具调用完成时调用，异常直接向上传播。
            running_loop: 保留的运行时参数；工具执行不通过事件循环广播运行期输出。

        返回:
            含观察列表与模型消息的 ``ToolRunResult``。观察与消息数量恒等于 ``calls``
            数量，且按入参原始顺序返回——未执行的 call（取消跳过）在其原始 index
            位置以取消占位补齐，保证每个 call_id 的 ``tool_calls`` 协议配对闭合。

        异常:
            生命周期回调抛出的异常会原样向上传播，确保 canonical 写入失败不会被静默
            忽略。单个工具失败或执行链内部异常均**不**向上抛出，而是由观察结果的
            ``status`` 表达。

        副作用:
            可能调用工具开始/完成生命周期回调；执行链内部异常时写 error 日志（含堆栈），
            本批因取消跳过调用时写 warning 日志。
        """

        if calls and (not callable(on_tool_call_started) or not callable(on_tool_call_finished)):
            raise RuntimeError(
                "tool lifecycle callbacks are required for a non-empty tool call batch"
            )

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
        # 执行与生命周期通知收口复用并行路径同一套辅助方法，串行/并行行为一致，
        # 避免平行复制回调调用与错误处理语义。
        for index, call in serial_calls:
            if self._should_cancel():
                break
            self._notify_tool_call_started(step_id, call, on_tool_call_started)
            observation = self._execute_tool_call(step_id, call, execution_context)
            indexed_observations.append((index, observation))
            self._notify_tool_call_finished(step_id, observation, on_tool_call_finished)

        # 并行组：统一交给并行执行器（该方法只处理并行调用）。取消已生效时并行组
        # 整体跳过，未执行的 call 由 _build_result_with_cancel_placeholders 补占位。
        if parallel_calls and not self._should_cancel():
            indexed_observations.extend(
                self._run_calls_with_parallel_modes(
                    step_id=step_id,
                    calls=parallel_calls,
                    execution_context=execution_context,
                    on_tool_call_started=on_tool_call_started,
                    on_tool_call_finished=on_tool_call_finished,
                )
            )

        # 配对闭合不变量收口：按原始 index 合并排序、为未执行的 call 补取消占位，
        # 并统一序列化为 role="tool" 模型消息（单一收口，避免平行复制语义漂移）。
        notified_ids = {observation.tool_call_id for _, observation in indexed_observations}
        result = self._build_result_with_cancel_placeholders(step_id, calls, indexed_observations)
        # 未执行的取消占位是在统一收口时创建的，此前没有机会触发生命周期回调；
        # 补发 finished，保证 canonical tool call 与模型上下文同样闭合。
        for observation in result.observations:
            if observation.tool_call_id not in notified_ids:
                self._notify_tool_call_finished(step_id, observation, on_tool_call_finished)
        return result

    def run_calls_with_events(
        self,
        step_id: str,
        calls: list[ToolCall],
        execution_context: ToolExecutionContext | None = None,
        running_loop: asyncio.AbstractEventLoop | None = None,
        **runtime_bridge: object,
    ) -> ToolRunResult:
        """兼容旧 RuntimeOperations 转发形态，但只接受新的生命周期回调容器。

        ``RuntimeOperations`` 当前仍以关键字把第四个位置的依赖转发到这里；该
        兼容入口不解析事件类型或载荷，只提取节点提供的
        ``on_tool_call_started`` / ``on_tool_call_finished`` 两个回调，然后委托给
        ``run_calls``。待 RuntimeOperations 完成同一迁移后可删除本入口。

        参数:
            step_id: 请求这些工具调用的步骤标识。
            calls: 模型请求的工具调用列表。
            execution_context: 本次执行的运行时边界。
            running_loop: 保留的运行时参数。
            runtime_bridge: RuntimeOperations 转发的生命周期回调容器。

        返回:
            ``run_calls`` 的工具执行结果。

        异常:
            生命周期回调缺失或回调写入失败时向上传播。

        副作用:
            委托 ``run_calls`` 执行工具并写入 canonical 工具事实。
        """
        if len(runtime_bridge) != 1:
            raise TypeError("exactly one tool lifecycle callback container is required")
        callback_container = next(iter(runtime_bridge.values()))
        if isinstance(callback_container, Mapping):
            started = callback_container.get("on_tool_call_started")
            finished = callback_container.get("on_tool_call_finished")
        else:
            started = getattr(callback_container, "on_tool_call_started", None)
            finished = getattr(callback_container, "on_tool_call_finished", None)
        if not callable(started) or not callable(finished):
            raise TypeError(
                "tool lifecycle callback container must provide callable started and finished "
                "callbacks"
            )
        return self.run_calls(
            step_id=step_id,
            calls=calls,
            execution_context=execution_context,
            on_tool_call_started=cast(ToolCallStartedCallback, started),
            on_tool_call_finished=cast(ToolCallFinishedCallback, finished),
            running_loop=running_loop,
        )

    def _run_calls_with_parallel_modes(
        self,
        step_id: str,
        calls: list[tuple[int, ToolCall]],
        execution_context: ToolExecutionContext | None,
        on_tool_call_started: ToolCallStartedCallback | None,
        on_tool_call_finished: ToolCallFinishedCallback | None,
    ) -> list[tuple[int, ToolObservation]]:
        """并发执行一批已声明为可并行调度的工具调用。

        只处理并行组：``run_calls_with_events`` 入口已按工具声明的调度模式完成分流，
        本方法不再包含串行分支；串行调用始终留在串行路径逐个执行。
        每次线程池提交前用 ``contextvars.copy_context()`` 复制当前线程 context（含父
        turn 根 observation 的 OTel current span），并经 ``ctx.run`` 包装提交，使 worker
        线程在捕获的 context 里执行工具——并行工具（尤其 ``delegate_task``）的 tool
        observation 与子 turn 由此正确嵌套在父 turn trace 下，而不是脱离为独立 trace。
        注意每个任务必须持有独立 context 副本，同一 Context 对象不可被并发进入。

        参数:
            step_id: 请求这些工具调用的步骤标识。
            calls: 带原始位置的并行工具调用列表（元组 ``(index, call)``）。
            execution_context: 本次工具执行上下文。
            on_tool_call_started: 工具调用开始回调。
            on_tool_call_finished: 工具调用完成回调。

        返回:
            带原始位置的已执行观察列表（按实际完成顺序）。批次中途取消时可能少于
            传入数量；未执行部分由调用方经 ``_build_result_with_cancel_placeholders``
            在原始 index 位置补取消占位。

        异常:
            无。worker 抛出的意外异常会被收口为对应 call 的 error 观察。

        副作用:
            启动临时线程池执行工具，并通知 started/finished 生命周期回调；
            每任务复制一份 contextvars 快照，不引入跨线程可变状态。
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
                    生命周期回调或线程池提交异常直接向外传播。

                副作用:
                    通知 started 回调，并向线程池提交新的工具调用。
                """
                while (
                    pending_calls
                    and len(future_by_call) < max_workers
                    and not self._should_cancel()
                ):
                    batch_index, batch_call = pending_calls.pop()
                    self._notify_tool_call_started(step_id, batch_call, on_tool_call_started)
                    # 每次提交前复制当前线程 contextvars（含父 turn 根 observation 的
                    # OTel current context）。ThreadPoolExecutor worker 默认在全新 context
                    # 运行，不复制会丢失 current span，导致 delegate 工具 span / 子 turn
                    # 脱离父 trace；且同一 Context 对象不能并发进入，必须逐任务独立副本。
                    future_by_call[
                        pool.submit(
                            contextvars.copy_context().run,
                            self._execute_tool_call,
                            step_id,
                            batch_call,
                            execution_context,
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
                    self._notify_tool_call_finished(step_id, observation, on_tool_call_finished)
                    completed.append((index, observation))
                _submit_until_full()
        return completed

    def _execute_tool_call(
        self,
        step_id: str,
        call: ToolCall,
        execution_context: ToolExecutionContext | None,
    ) -> ToolObservation:
        """执行单个工具调用，并把执行链路异常收口为工具观察。

        参数:
            step_id: 请求该工具调用的步骤标识。
            call: 当前工具调用。
            execution_context: 本次工具执行上下文。

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

    def _notify_tool_call_started(
        self,
        step_id: str,
        call: ToolCall,
        callback: ToolCallStartedCallback | None,
    ) -> None:
        """通知工具调用开始，canonical 写入失败时直接向上传播。

        参数:
            step_id: 请求该工具调用的步骤标识。
            call: 当前工具调用。
            callback: 工具调用开始生命周期回调。

        返回:
            无。

        异常:
            透传 callback 异常，不能把 canonical 写入失败降级为工具执行成功。

        副作用:
            调用 callback。
        """
        if callback is not None:
            callback(step_id, call)

    def _notify_tool_call_finished(
        self,
        step_id: str,
        observation: ToolObservation,
        callback: ToolCallFinishedCallback | None,
    ) -> None:
        """通知工具调用完成，canonical 写入失败时直接向上传播。

        参数:
            step_id: 请求该工具调用的步骤标识。
            observation: 工具执行观察结果。
            callback: 工具调用完成生命周期回调。

        返回:
            无。

        异常:
            透传 callback 异常，不能把 canonical 写入失败降级为工具执行成功。

        副作用:
            调用 callback。
        """
        if callback is not None:
            callback(step_id, observation)

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
            (index, call) for index, call in enumerate(calls) if index not in executed_indices
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
                            reason=CANCEL_NOT_EXECUTED_REASON,
                            error="the tool call was cancelled before execution",
                            tool_call_id=call.call_id,
                        ),
                    )
                )
        indexed_observations.sort(key=lambda item: item[0])
        observations = [observation for _, observation in indexed_observations]
        messages = [self._to_model_message(observation) for observation in observations]
        return ToolRunResult(observations=observations, messages_for_model=messages)

    def build_cancel_placeholder_messages(
        self,
        calls: list[ToolCall],
    ) -> list[RuntimeMessage]:
        """为一批未执行的工具调用构造取消占位消息（供执行前分支复用）。

        把「未执行调用 → cancelled 占位观察 → 序列化模型消息」的配对闭合逻辑
        收敛到 service 单一实现，避免 ``core`` 编排层钻入受保护成员自拼占位。
        与 ``_build_result_with_cancel_placeholders`` 内部使用的工厂与序列化完全同源，
        保证正常路径（执行中取消）与 ``tools_node`` 执行前整批取消两条分支产出的
        协议字段一致。

        参数:
            calls: 需要补占位的工具调用列表（已确定不会执行）。

        返回:
            按入参顺序排列、可直接写回运行时上下文的 ``role="tool"`` 消息列表。

        异常:
            无。

        副作用:
            序列化时会就地清空每个占位的 ``display_data``（经 ``_to_model_message``）。
        """
        placeholders = [
            self._to_model_message(
                tool_cancelled(
                    tool_name=call.tool_name,
                    reason=CANCEL_NOT_EXECUTED_REASON,
                    error="the tool call was cancelled before execution",
                    tool_call_id=call.call_id,
                )
            )
            for call in calls
        ]
        return placeholders

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
        """把工具观察序列化为模型可见的 ``role="tool"`` 消息（markdown 结构）。

        ``content_text`` 以 markdown 区块组织**对模型可见**的字段：``## Tool`` 承载
        ``tool_name`` / ``status`` / ``retryable``，``## Output`` 承载 ``content``，
        ``## Error`` 承载 ``error``，``## Reason`` 承载 ``reason``；空值跳过对应区块。
        ``tool_call_id``（置于 ``metadata``）、``data``、``permission`` 对模型不可见。

        参数:
            observation: 已产出的工具观察（含正常结果、错误占位、取消占位）。

        返回:
            可并入模型上下文的 ``RuntimeMessage``，``metadata.tool_call_id`` 用于与
            ``AIMessage.tool_calls`` 配对闭合。

        异常:
            无。

        副作用:
            无（不修改入参观察对象）。
        """
        # 仅向模型暴露面向人读的文本通道（content / error / reason）与执行元信息
        # （tool_name / status / retryable）。tool_call_id / data / permission 对模型不可见
        # （前者在 metadata、后者转模型前已被 clear_display_data 清空）。None 与空串视为
        # 无信息，跳过对应区块。其余字段以 markdown 结构组织，使模型能区分「元信息 / 输出 /
        # 错误 / 修正建议」四个语义维度。
        sections: list[str] = []
        meta_lines: list[str] = []
        if observation.tool_name:
            meta_lines.append(f"- name: {observation.tool_name}")
        if observation.status:
            meta_lines.append(f"- status: {observation.status}")
        meta_lines.append(f"- retryable: {observation.retryable}")
        if meta_lines:
            sections.append("## Tool\n\n" + "\n".join(meta_lines))
        if observation.content:
            sections.append(f"## Output\n\n{observation.content}")
        if observation.error:
            sections.append(f"## Error\n\n{observation.error}")
        if observation.reason:
            sections.append(f"## Reason\n\n{observation.reason}")
        content_text = "\n\n".join(sections)
        return RuntimeMessage(
            role="tool",
            content_text=content_text,
            metadata={"tool_call_id": observation.tool_call_id},
        )
