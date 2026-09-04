"""工具执行批次结果值对象。

单一职责：承载一次模型请求的工具调用批次执行结果（观察列表 + 供下一步模型使用的消息）。
不负责执行细节（由 ``ToolExecutionService`` 编排、``ToolScheduler`` 执行）。
"""

from dataclasses import dataclass

from langchain_core.messages import BaseMessage

from app.core.tools.schemas import ToolObservation


@dataclass
class ToolRunResult:
    """一次工具调用批次的执行结果。"""

    observations: list[ToolObservation]
