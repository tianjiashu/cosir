"""工具执行编排服务。

单一职责：编排一次模型请求的工具调用批次执行，并产出供下一步模型使用的观察结果与消息。
权限校验委托给 ``ToolScheduler``（其 ``execute`` 已按策略返回 ``permission_denied`` /
``unknown_tool`` / ``invalid_arguments`` 等观察）。

职责边界：
- 负责：批量执行工具调用、发出工具生命周期事件、把观察结果转为模型消息。
- 不负责：工具注册、参数校验细节、子进程隔离（均由 ``ToolScheduler`` / ``ToolExecutor`` 负责）。
"""

import logging
from collections.abc import Callable

from app.models import RuntimeMessage
from app.models.enums.event_type import EventType
from app.service.tool_execution.run_result import ToolRunResult
from app.tools.schemas import ToolCall
from app.tools.tool_execute.tool_scheduler import ToolScheduler


class ToolExecutionService:
    """Orchestrate a batch of tool calls requested by the model."""

    def __init__(self, scheduler: ToolScheduler, agent_id: str, logger: logging.Logger) -> None:
        """Initialize the tool execution service.

        参数:
            scheduler: 底层工具调度器（负责校验与执行）。
            agent_id: 执行主体标识（用于日志关联）。
            logger: 运行时日志器。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self._scheduler = scheduler
        self._agent_id = agent_id
        self._logger = logger

    def run_calls_with_events(
        self,
        task_id: str,
        step_id: str,
        calls: list[ToolCall],
        write_event: Callable | None = None,
    ) -> ToolRunResult:
        """执行一批工具调用并发出生命周期事件。

        每个调用经 ``ToolScheduler.execute`` 执行（其内部完成权限与参数校验），观察结果
        转为 ``role="tool"`` 的 ``RuntimeMessage`` 供下一步模型消费；若提供 ``write_event``
        回调，则对每个完成的工具调用发出 ``TOOL_CALL_FINISHED`` 事件。

        参数:
            task_id: 当前任务标识（用于日志关联）。
            step_id: 请求这些工具调用的步骤标识。
            calls: 模型请求的工具调用列表。
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
            observation = self._scheduler.execute(call)
            observations.append(observation)
            if write_event is not None:
                write_event(
                    EventType.TOOL_CALL_FINISHED,
                    {
                        "step_id": step_id,
                        "tool_name": observation.tool_name,
                        "status": observation.status,
                        "tool_call_id": observation.tool_call_id,
                    },
                )
            messages.append(
                RuntimeMessage(
                    role="tool",
                    content_text=observation.content,
                    metadata={"tool_call_id": observation.tool_call_id},
                )
            )
        return ToolRunResult(observations=observations, messages_for_model=messages)
