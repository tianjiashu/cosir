"""工具执行编排服务。

单一职责：编排一次模型请求的工具调用批次执行，并产出供下一步模型使用的观察结果与消息。
权限校验委托给 ``ToolScheduler``（其 ``execute`` 已按策略返回 ``permission_denied`` /
``unknown_tool`` / ``invalid_arguments`` 等观察）。

职责边界：
- 负责：批量执行工具调用、发出工具生命周期事件、把观察结果转为模型消息。
- 不负责：工具注册、参数校验细节、子进程隔离（均由 ``ToolScheduler`` / ``ToolExecutor`` 负责）；
  也不负责任何渲染——事件只透传工具的静态展示声明与结构化数据，摘要与条目由客户端生成。
"""

import asyncio
import dataclasses
import json
from collections.abc import Callable, Iterable

from app.config.logging.logger import log
from app.models import RuntimeMessage
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
            数量，且与入参顺序一致（已执行的在前、因取消跳过而补的占位在后），保证
            每个 call_id 的 ``tool_calls`` 协议配对闭合。

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

        observations: list[ToolObservation] = []
        messages: list[RuntimeMessage] = []
        executed_call_ids: set[str] = set()
        # 当前串行执行，后续可并行
        for call in calls:
            if self._should_cancel():
                break
            display: ToolDisplayHints | None = self._display_by_name.get(call.tool_name)
            display_payload = dataclasses.asdict(display) if display is not None else None

            # 工具执行开始事件
            write_event(
                EventType.TOOL_CALL_STARTED,
                ToolCallStartedPayload(
                    tool_name=call.tool_name,
                    step_id=step_id,
                    tool_call_id=call.call_id,
                    arguments=call.arguments if isinstance(call.arguments, dict) else {},
                    display=display_payload,
                ),
            )

            # 执行工具调用（包在可选 trace span 内，记录参数/结果/耗时；缺省为空实现）。
            # 捕获执行链自身的意外异常（调度器/事件/trace span/序列化等内部 bug，而非
            # 工具 handler 主动返回的业务失败）：此时工具本体未运行，须为当前 call 补一个
            # 结构化 error 占位，让模型感知「内部执行错误」而非悬空崩协议。
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
            except Exception as exc:  # 执行链 bug 必须收口为 error 观察
                log.error(
                    "tool_call_internal_error",
                    extra={
                        "msg": "工具调用执行链内部异常，已收口为 error 观察",
                        "data": {
                            "tool_name": call.tool_name,
                            "tool_call_id": call.call_id,
                            "step_id": step_id,
                            "error": str(exc),
                        },
                    },
                    exc_info=True,
                )
                # 仅用异常类型名构造面向模型的说明，避免把未脱敏的 exc 原文（可能含路径 /
                # 凭据 / 命令行片段）回传模型或事件流；完整原文已由上方 error 日志 exc_info 承载。
                header = f"internal execution error before the tool ran: {type(exc).__name__}"
                observation = tool_error(
                    tool_name=call.tool_name,
                    error=header,
                    reason=internal_execution_error_reason(header),
                    retryable=False,
                    tool_call_id=call.call_id,
                )
            # 记录观察结果
            observations.append(observation)
            executed_call_ids.add(call.call_id)

            # 工具执行结束事件：只透传结构化数据，摘要与展示条目由客户端渲染
            write_event(
                EventType.TOOL_CALL_FINISHED,
                ToolCallFinishedPayload(
                    step_id=step_id,
                    tool_name=observation.tool_name,
                    status="success" if observation.status == "success" else "error",
                    tool_call_id=observation.tool_call_id,
                    content=observation.content,
                    error=observation.error,
                    reason=observation.reason,
                    retryable=observation.retryable,
                    data=observation.data or {},
                ),
            )

            # 运行中实时广播：本工具调用产生文件变更时，广播 FILE_CHANGE_UPDATED 驱动
            # 前端即时展示（不持久化到 runtime_events，数据源仍在 file_snapshots 表）。
            # 采集在 FileSnapshotHook（POST_TOOL_USE 内置 Hook）完成（含 stable=0 运行中态）。
            # 缺 event_bus 或 loop 时跳过（降级为仅全量查询可见）。
            if (
                self._event_bus is not None
                and observation.status == "success"
                and execution_context is not None
                and execution_context.task_id
                and execution_context.turn_id
            ):
                self._publish_file_change_updated(execution_context, observation, loop)

            # 转为模型消息（统一经 _to_model_message，确保 display_data 清空与脱敏一致）。
            messages.append(self._to_model_message(observation))

        # 配对闭合不变量：对未被执行的 call（取消跳过 / 未进入循环）补占位 error 观察，
        # 使模型感知「这一步因取消而没有运行」，并闭合 tool_calls 协议避免下一轮对话崩溃。
        skipped_calls = [c for c in calls if c.call_id not in executed_call_ids]
        if skipped_calls:
            log.warning(
                "tool_calls_cancelled_not_executed",
                extra={
                    "msg": "本批工具调用因取消未执行，已补 error 占位闭合协议",
                    "data": {
                        "step_id": step_id,
                        "total": len(calls),
                        "executed": len(executed_call_ids),
                        "skipped_call_ids": [c.call_id for c in skipped_calls],
                    },
                },
            )
            for call in skipped_calls:
                observation = tool_error(
                    tool_name=call.tool_name,
                    error="the tool call was cancelled before execution",
                    reason=cancel_not_executed_reason(),
                    retryable=False,
                    tool_call_id=call.call_id,
                )
                observations.append(observation)
                messages.append(self._to_model_message(observation))
        return ToolRunResult(observations=observations, messages_for_model=messages)

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
            diff_results = [
                FileDiffResult(
                    path=str(change.get("path", "")),
                    status=str(change.get("status") or "modified"),
                    before=str(change.get("before") or ""),
                    after=str(change.get("after") or ""),
                )
                for change in changes
                if isinstance(change, dict)
            ]
            stats = build_diff_stats(diff_results)
            stat_by_index = dict(enumerate(stats.get("files", [])))
            for index, change in enumerate(changes):
                if not isinstance(change, dict):
                    continue
                path = change.get("path")
                action = change.get("action") or change.get("status")
                if not path or not action:
                    continue
                file_stat = stat_by_index.get(index, {})
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
