"""工具执行编排服务。

单一职责：编排一次模型请求的工具调用批次执行，并产出供下一步模型使用的观察结果与消息。
权限校验委托给 ``ToolScheduler``（其 ``execute`` 已按策略返回 ``permission_denied`` /
``unknown_tool`` / ``invalid_arguments`` 等观察）。

职责边界：
- 负责：批量执行工具调用、发出工具生命周期事件、把观察结果转为模型消息。
- 不负责：工具注册、参数校验细节、子进程隔离（均由 ``ToolScheduler`` / ``ToolExecutor`` 负责）；
  也不负责任何渲染——事件只透传工具的静态展示声明与结构化数据，摘要与条目由客户端生成。
"""

import dataclasses
import json
from collections.abc import Callable, Iterable

from app.config.logging.logger import log
from app.models import RuntimeMessage
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload import ToolCallFinishedPayload, ToolCallStartedPayload
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
from app.tools.tool_execute.tool_scheduler import ToolScheduler
from app.tools.tool_handler.patch.patch_diff import FileDiffResult, build_diff_stats
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
            event_bus: 可选的运行时事件总线；提供时，每次产生文件变更的工具调用完成后
                广播 ``FILE_CHANGE_UPDATED``（不持久化，仅驱动前端实时展示运行中变更）。

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
    ) -> ToolRunResult:
        """执行一批工具调用并发出生命周期事件。

        每个调用经 ``ToolScheduler.execute`` 执行（其内部完成权限与参数校验），观察结果
        转为 ``role="tool"`` 的 ``RuntimeMessage`` 供下一步模型消费；若提供 ``write_event``
        回调，则对每个完成的工具调用发出 ``TOOL_CALL_FINISHED`` 事件。

        参数:
            step_id: 请求这些工具调用的步骤标识。
            calls: 模型请求的工具调用列表。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                透传给 ``ToolScheduler.execute``，最终在执行期注入 handler。
            write_event: 可选的工具生命周期事件写入回调。

        返回:
            含观察列表与模型消息的 ``ToolRunResult``。

        异常:
            无（单个工具失败由观察结果的 ``status`` 表达，不向上抛出）。

        副作用:
            可能通过 ``write_event`` 写入事件；可能记工具执行日志。
        """

        if write_event is None:
            raise RuntimeError("write_event is None")

        observations = []
        messages: list[RuntimeMessage] = []
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
            with self._trace_recorder.span(call, step_id) as tool_span:
                observation = self._scheduler.execute(
                    call,
                    execution_context=execution_context,
                    allowed_tool_names=self._allowed_tool_names,
                    should_cancel=self._should_cancel,
                )
                tool_span.record(observation)
            # 记录观察结果
            observations.append(observation)

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
            # 采集在 ToolScheduler._record_file_snapshot 完成（含 stable=0 运行中态），
            # 此处与 TOOL_CALL_FINISHED 同级、确定在事件循环协程栈上广播。无 event_bus
            # 时跳过（降级为仅全量查询可见）。
            if (
                self._event_bus is not None
                and observation.status == "success"
                and execution_context is not None
                and execution_context.task_id
                and execution_context.turn_id
            ):
                self._publish_file_change_updated(execution_context, observation)

            # 转为模型消息,display_data 不可以给模型看。
            observation.clear_display_data()
            serialized = dataclasses.asdict(observation)
            serialized["content"] = redact_terminal_output(observation.content)
            messages.append(
                RuntimeMessage(
                    role="tool",
                    content_text=json.dumps(
                        {k: v for k, v in serialized.items() if v is not None},
                        ensure_ascii=False,
                    ),
                    metadata={"tool_call_id": observation.tool_call_id},
                )
            )
        return ToolRunResult(observations=observations, messages_for_model=messages)

    def _publish_file_change_updated(
        self,
        execution_context: ToolExecutionContext,
        observation: ToolObservation,
    ) -> None:
        """广播本次工具调用产生的文件变更，驱动前端运行中实时展示。

        参数:
            execution_context: 本次执行的运行时边界（含 task_id / turn_id）。
            observation: 工具观察结果；其 ``data["changes"]`` 为单文件变更字典列表，
                每个含 ``path`` / ``before`` / ``after`` 与变更动作
                （``action`` 或 ``status`` 字段）。

        返回:
            无。

        异常:
            无。广播属展示侧增强，失败不应影响工具执行主流程，故整体捕获并记 warning。

        副作用:
            经注入的 ``RuntimeEventBus`` 发布若干条不持久化的 ``FILE_CHANGE_UPDATED`` 事件，
            每条携带该文件实时 diff（additions / deletions / before / after），供前端零延迟渲染。
        """
        bus = self._event_bus
        if bus is None:
            return
        changes = (observation.data or {}).get("changes")
        if not isinstance(changes, list) or not changes:
            return
        try:
            # 复用既有 diff 统计能力计算每文件增删行数，避免重复实现差异算法。
            # 与采集层 ``_change_diff_stats`` 保持同一语义（status 映射由 build_diff_stats 处理）。
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
                bus.publish(
                    RuntimeEvent(
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
                )
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
