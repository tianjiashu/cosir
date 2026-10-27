"""HITL 工具等待用户决定时的占位观察。"""

from collections.abc import Mapping
from typing import Any

from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolObservation

_AWAITING_DECISION_CONTENT = (
    "This tool call is awaiting the user's decision. Do not call it again or assume the outcome. "
    "You are paused until the user decides, and the result will be delivered to you as a "
    "separate message. If another message wakes you before that, handle it, but do not repeat "
    "this call."
)


def tool_awaiting_decision(
    tool_name: str,
    tool_call_id: str,
    display_data: Mapping[str, Any] | None = None,
) -> ToolObservation:
    """构造 HITL 工具首次调用的等待占位：挂起 Agent，并告知模型结果会另行送达。

    占位是这次调用在模型协议上的最终结果：允许其他消息在等待期间唤醒 Run，模型据此知道不要重复
    调用；用户决定后的执行结果作为新消息追加，不覆盖它，历史前缀因此不变。所有 HITL 工具共用
    这段文案。
    """
    observation = tool_success(
        tool_name,
        _AWAITING_DECISION_CONTENT,
        tool_call_id=tool_call_id,
        display_data=display_data,
        suspend=True,
    )
    observation.awaiting_decision = True
    return observation
