"""ReAct-like 工作流的 graph state 定义。

本模块只承载交给 LangGraph 管理的 graph state 数据契约与追加式 reducer，不依赖任何
节点、边或编排逻辑。state 是 graph 各节点之间传递的唯一数据通道。
"""

from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from pydantic import BaseModel


def _add_messages(
    existing: list[BaseMessage] | None, new: list[BaseMessage] | None
) -> list[BaseMessage]:
    """追加式合并 graph 消息通道。

    每个节点只返回本次新增的消息，reducer 负责把它们追加到已有上下文之后，
    供后续模型步骤继续推理。

    参数:
        existing: 通道中已存在的消息列表。
        new: 节点本次返回的新增消息列表。

    返回:
        合并后的完整消息列表。
    """

    return (existing or []) + (new or [])


class ReactGraphState(BaseModel):
    """ReAct-like 工作流交给 LangGraph 管理的 graph state。

    Attributes:
        messages: 模型上下文消息列表，使用追加式 reducer 累积。
        step_count: 已执行的模型步骤数量，用于配合 ``max_steps`` 防止无限循环。
        tool_error_count: 连续工具执行失败次数，任意一次成功工具调用都会重置。
        requested_tool: 当前模型步骤是否请求了工具调用。
        final_response: 当前模型步骤是否已经产出最终回答。
        terminal: 当前工作流是否已经进入完成、失败或取消等终止状态。
        pending_tool_calls: 模型请求、待执行的工具调用（以可序列化的 dict 列表存储，在 tools 节点消费）。
        max_steps: 本轮允许的最大模型步骤数。
        final_text: 模型产出的最终回答文本，供 checkpoint 重放时恢复。
    """

    messages: Annotated[list[BaseMessage], _add_messages]
    step_count: int
    tool_error_count: int
    requested_tool: bool
    final_response: bool
    terminal: bool
    pending_tool_calls: list[dict[str, Any]]
    max_steps: int
    final_text: str
