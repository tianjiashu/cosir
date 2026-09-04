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
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.runtime.tool_execution.run_result import ToolRunResult
from app.core.runtime.tool_execution.tool_trace_recorder import (
    ToolTraceRecorder,
    _NullToolTraceRecorder,
)
from app.core.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import (
    internal_execution_error_reason,
    tool_error,
)
from app.core.tools.tool_execute.tool_scheduler import ToolScheduler
from app.models.enums.error_kind import ErrorKind

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
        running_loop: asyncio.AbstractEventLoop | None = None,
    ) -> ToolRunResult:
        """执行一批工具调用并通知明确的工具生命周期回调。

        每个调用经 ``ToolScheduler.execute`` 执行（其内部完成权限与参数校验），观察结果
        转为 ``ToolMessage`` 供下一步模型消费。开始与完成回调只
        传递工具执行事实，不依赖通用运行时事件信封。

        入口按工具声明的调度模式（``ToolDefinition.parallel_mode``）分流：串行调用留在
        本方法的串行路径逐个执行；声明为 ``parallel`` 的调用统一交给
        ``_run_calls_with_parallel_modes`` 并发执行（该方法只处理并行组，不再混入串行
        分支）。两条路径都保留原始 index；已执行观察由本方法统一合并、序列化为模型消息，
        未执行的 call 不产消息，其协议配对闭合由 ``RuntimeContextManager.load_message`` 统一收口。

        配对闭合不变量：模型协议层面 ``AIMessage.tool_calls`` 的每个 call 必须最终配对一条
        ``role="tool"`` 消息，否则下一轮对话会因协议不匹配崩溃。该不变量已下沉到
        ``RuntimeContextManager.load_message`` 统一收口（崩溃/取消遗留的悬空调用在下次取数时
        自动补 ``ToolMessage`` 占位）。本方法只负责已执行观察的合并与序列化，并为取消跳过的
        call 补 canonical 审计终态（``status="cancelled"``），不在此处产模型消息。

        参数:
            step_id: 请求这些工具调用的步骤标识。
            calls: 模型请求的工具调用列表。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                透传给 ``ToolScheduler.execute``，最终在执行期注入 handler。
            on_tool_call_started: 工具调用开始时调用，异常直接向上传播。
            on_tool_call_finished: 工具调用完成时调用，异常直接向上传播。
            running_loop: 保留的运行时参数；工具执行不通过事件循环广播运行期输出。

        返回:
            含已执行观察列表与其模型消息的 ``ToolRunResult``。未执行的 call（取消跳过）
            不进入返回，其协议配对闭合由 ``RuntimeContextManager.load_message`` 在下次
            模型取数时统一补充 ``ToolMessage`` 占位；canonical 审计终态由本方法同步写入。

        异常:
            生命周期回调抛出的异常会原样向上传播，确保 canonical 写入失败不会被静默
            忽略。单个工具失败或执行链内部异常均**不**向上抛出，而是由观察结果的
            ``status`` 表达。

        副作用:
            可能调用工具开始/完成生命周期回调；执行链内部异常时写 error 日志（含堆栈），
            本批因取消跳过调用时写 warning 日志。
        """

        # 入口分流：按工具声明的调度模式，把本批调用拆成「串行组」与「并行组」。
        # 串行组保留原始相对顺序逐个执行；并行组统一交给 _run_calls_with_parallel_modes
        # 并发执行（该方法只处理并行调用，不再混入串行分支）。两组都保留原始 index，
        # 已执行观察由本方法统一合并、序列化为模型消息（协议配对闭合由
        # RuntimeContextManager.load_message 在下次取数时统一收口）。
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
            observation = self._execute_tool_call(step_id, call, execution_context)
            indexed_observations.append((index, observation))


        # 并行组：统一交给并行执行器（该方法只处理并行调用）。取消已生效时并行组
        # 整体跳过，未执行的 call 仅补 canonical 审计终态（协议配对闭合由
        # RuntimeContextManager.load_message 在下次取数时统一收口）。
        if parallel_calls and not self._should_cancel():
            indexed_observations.extend(
                self._run_calls_with_parallel_modes(
                    step_id=step_id,
                    calls=parallel_calls,
                    execution_context=execution_context,
                )
            )

        executed_observations = [observation for _, observation in indexed_observations]


        return ToolRunResult(
            observations=executed_observations
        )


    def _run_calls_with_parallel_modes(
        self,
        step_id: str,
        calls: list[tuple[int, ToolCall]],
        execution_context: ToolExecutionContext | None,
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
            传入数量；未执行部分由调用方补 canonical 审计终态，其协议配对闭合由
            ``RuntimeContextManager.load_message`` 在下次取数时统一收口。

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
                    # 每次提交前复制当前线程 contextvars（含父 turn 根 observation 的
                    # OTel current context）。ThreadPoolExecutor worker 默认在全新 context
                    # 运行，不复制会丢失 current span，导致 delegate 工具 span / 子 turn
                    # 脱离父 trace；且同一 Context 对象不能并发进入，必须逐任务独立副本。
                    # 复制上下文并按任务独立绑定，使并行 worker 在父 turn 的 contextvars
                    # （含 OTel current span）下执行，避免 delegate 工具 span / 子 turn 脱离父 trace。
                    # 注意：下面 pool.submit 的实参搭配会触发 Pyright 对 Context.run 的
                    # ParamSpec 与 ThreadPoolExecutor.submit 泛型匹配误报（运行期语义正确），
                    # 用 pyright 抑制注释明确标注，不改动真实逻辑。
                    run_ctx = contextvars.copy_context()
                    future_by_call[
                        pool.submit(  # pyright: ignore
                            run_ctx.run,
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
