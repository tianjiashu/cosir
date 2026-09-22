"""统一构造工具取消观察。

取消是用户或系统主动中止，不是工具执行错误。默认文案由本模块统一提供，调用方可以传入更
精确的取消来源说明覆盖它，但不得借此塞入错误详情（取消不是失败）。
"""

import asyncio

from app.core.tools.schemas import ToolObservation


class ToolCallCancelled(asyncio.CancelledError):
    """Internal marker separating tool-call cancellation from run cancellation."""

CANCELLED_REASON = "the tool call was cancelled before completion; no result was produced."


def tool_cancelled(
    tool_name: str, permission: str = "", tool_call_id: str = "", reason: str = ""
) -> ToolObservation:
    """构造统一的取消态工具观察。

    参数:
        tool_name: 被取消的工具名称。
        permission: 触发工具所需的权限标识。
        tool_call_id: 关联的模型工具调用 id。
        reason: 覆盖默认取消文案的 ``reason``；为空字符串时使用模块常量
            ``CANCELLED_REASON``。工具执行层取消会传入点名取消来源的富文本，使模型能区分
            「用户中止」与「确定性失败」。该值只面向模型，不进入展示通道。

    返回:
        ``status="cancelled"``、``content=None``、``error=None``、``retryable=False`` 的
        :class:`ToolObservation`；``reason`` 按上述规则取默认值或调用方传入值。其中
        ``retryable=False`` 仅为兼容字段，取消消息不使用错误重试语义。

    异常:
        无。

    副作用:
        无。
    """

    return ToolObservation(
        tool_name=tool_name,
        status="cancelled",
        content=None,
        error=None,
        reason=CANCELLED_REASON if reason == "" else reason,
        retryable=False,
        permission=permission,
        tool_call_id=tool_call_id,
    )
