"""统一构造工具取消观察。

取消是用户或系统主动中止，不是工具执行错误。默认文案由本模块统一提供，调用方可以传入更
精确的取消来源说明覆盖它，但不得借此塞入错误详情（取消不是失败）。
"""

from app.core.tool.schemas import ToolObservation

CANCELLED_REASON = "the tool call was cancelled before completion; no result was produced."


def tool_cancelled(
    tool_name: str, tool_call_id: str = "", reason: str = ""
) -> ToolObservation:
    """构造统一的取消态工具观察。

    参数:
        tool_name: 被取消的工具名称。
        tool_call_id: 关联的模型工具调用 id。
        reason: 覆盖默认取消文案；为空字符串时使用 ``CANCELLED_REASON``。工具执行层取消会
            传入点名取消来源的富文本，使模型能区分「用户中止」与「确定性失败」。

    返回:
        ``status="cancelled"`` 的观察，``content`` 为 ``cancelled: <reason>``：取消不是失败，
        文本里不带错误事实与重试提示。
    """

    return ToolObservation(
        tool_name=tool_name,
        status="cancelled",
        content=f"cancelled: {reason or CANCELLED_REASON}",
        tool_call_id=tool_call_id,
    )
