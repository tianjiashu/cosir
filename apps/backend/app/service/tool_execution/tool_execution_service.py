"""工具执行编排服务。

单一职责：编排一次模型请求的工具调用批次执行，并产出供下一步模型使用的观察结果与消息。
权限校验委托给 ``ToolScheduler``（其 ``execute`` 已按策略返回 ``permission_denied`` /
``unknown_tool`` / ``invalid_arguments`` 等观察）。

职责边界：
- 负责：批量执行工具调用、发出工具生命周期事件、把观察结果转为模型消息。
- 不负责：工具注册、参数校验细节、子进程隔离（均由 ``ToolScheduler`` / ``ToolExecutor`` 负责）。
"""

import dataclasses
import json
from collections.abc import Callable, Iterable
from typing import Literal

from app.models import RuntimeMessage
from app.models.enums.event_type import EventType
from app.models.payload import ToolCallFinishedPayload
from app.models.payload.runtime_event_payload import RuntimeEventPayload
from app.service.tool_execution.run_result import ToolRunResult
from app.tools.schemas import ToolCall, ToolExecutionContext
from app.tools.tool_execute.tool_scheduler import ToolScheduler
from app.trace_infra.redaction import redact_terminal_output


class ToolExecutionService:
    """Orchestrate a batch of tool calls requested by the model."""

    def __init__(
        self,
        scheduler: ToolScheduler,
        agent_id: str,
        allowed_tool_names: Iterable[str] | None = None,
    ) -> None:
        """Initialize the tool execution service.

        参数:
            scheduler: 底层工具调度器（负责校验与执行）。
            agent_id: 执行主体标识（用于日志关联）。
            allowed_tool_names: 当前 Agent profile 允许执行的工具名。

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

    def run_calls_with_events(
        self,
        task_id: str,
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
            task_id: 当前任务标识（用于日志关联）。
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

        observations = []
        messages: list[RuntimeMessage] = []
        for call in calls:
            observation = self._scheduler.execute(
                call,
                execution_context=execution_context,
                allowed_tool_names=self._allowed_tool_names,
            )
            observations.append(observation)
            status: Literal["success", "error"] = (
                "success" if observation.status == "success" else "error"
            )
            if write_event is not None:
                write_event(
                    EventType.TOOL_CALL_FINISHED,
                    ToolCallFinishedPayload(
                        step_id=step_id,
                        tool_name=observation.tool_name,
                        status=status,
                        tool_call_id=observation.tool_call_id,
                    ),
                )
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
