"""统一构造工具取消观察。

取消是用户或系统主动中止，不是工具执行错误。该模块统一填充取消观察的 reason，
不承载调用方传入的错误详情或自定义取消原因。
"""

from app.core.tools.schemas import ToolObservation

CANCELLED_REASON = "the tool call was cancelled before completion; no result was produced."


def tool_cancelled(
    tool_name: str,
    permission: str = "",
    tool_call_id: str = "",
) -> ToolObservation:
    """构造统一的取消态工具观察。

    参数:
        tool_name: 被取消的工具名称。
        permission: 触发工具所需的权限标识。
        tool_call_id: 关联的模型工具调用 id。

    返回:
        ``status="cancelled"``、统一 ``reason``、``content=None``、``error=None``、
        ``retryable=False`` 的 ``ToolObservation``。其中 ``retryable=False`` 仅为
        兼容字段，取消消息不使用错误重试语义。

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
        reason=CANCELLED_REASON,
        retryable=False,
        permission=permission,
        tool_call_id=tool_call_id,
    )
