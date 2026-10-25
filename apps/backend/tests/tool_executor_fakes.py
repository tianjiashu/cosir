"""执行器替身共用的事件辅助：替身按真实 ``ToolExecutor`` 的契约自行发出 running。

真实执行器在通过准入、即将交给 handler 时经 ``tool_executor.emit`` 发 running；用例把
``tool_executor.emit`` 与 ``tool_batch_runner.emit`` 补成同一个记录器，替身调用本辅助即可让
running 与批次入口发出的终态落在同一时间线上。
"""

from __future__ import annotations

import json

from app.core.events import ToolCallStatusChangedEvent
from app.core.tool.schemas import ToolCall, ToolExecutionContext
from app.core.tool.execute import tool_executor as tool_executor_module


def emit_running(call: ToolCall, execution_context: ToolExecutionContext) -> None:
    """以真实执行器的同一出口与字段发出该调用的 running 事件。"""

    tool_executor_module.emit(
        ToolCallStatusChangedEvent(
            task_id=execution_context.task_id,
            run_id=execution_context.run_id,
            step_id=execution_context.step_id,
            tool_call_id=execution_context.tool_call_id,
            status="running",
            # 真实执行器发的是准入后的参数；替身不过门禁，按合法 JSON 直接解码。
            args=json.loads(call.arguments),
        )
    )
