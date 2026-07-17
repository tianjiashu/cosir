"""归一化工具执行结果值对象。"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ToolObservation:
    """表示一个归一化的工具执行结果。

    参数:
        tool_name: 被执行工具的名称。
        status: 执行状态，例如 ``success`` 或 ``error``。
        content: 返回给运行时的文本观测结果。
        error: 当 status 为 ``error`` 时的可选错误消息。
        permission: 与该工具关联的权限级别。
        approval_status: 该工具调用的审批决定状态。

    返回:
        一个归一化的工具观测值对象。

    异常:
        无。

    副作用:
        无。
    """

    tool_name: str
    status: str
    content: str
    error: str = ""
    permission: str = ""
    approval_status: str = ""
    tool_call_id: str = ""
    artifact_id: Optional[str] = None
