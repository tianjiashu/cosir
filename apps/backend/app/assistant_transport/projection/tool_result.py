"""工具 metadata 到卡片字段的唯一转换，不读取正文、注册表或业务数据库。"""

from copy import deepcopy
from typing import NotRequired, TypedDict

from app.models.enums.tool_call_status import ToolCallEventStatus
from app.models.message_metadata import ToolMessageMetadata

# 展示数据里带子 Task 定位的两种 kind：``delegation-result`` 与 ``child-agent-result`` 的 ``send``
# （``status`` 操作是一次性查询，不是活的子 Task 引用）。
_CHILD_TASK_KINDS = frozenset({"delegation-result", "child-agent-result"})


class ToolResultFields(TypedDict):
    status: ToolCallEventStatus
    error: str | None
    isError: bool
    awaitingUser: bool
    display_data: dict[str, object] | None
    team_run_id: NotRequired[int]
    background: NotRequired[bool]
    child_task_id: NotRequired[int]
    agent_role: NotRequired[str]


def project_tool_result(metadata: ToolMessageMetadata) -> ToolResultFields:
    """实时状态与冷重建采用同一错误口径和关联信息，返回副本避免污染持久事实。

    ``awaitingUser`` 只在这里计算：占位行 ``pending`` 且不是后台占位，就是「等你操作」（HITL 等
    决定、交接等交还）。每次都显式写，决定到达、重放写回终态时自然清回 ``False``。
    """
    fields = ToolResultFields(
        status=metadata.status,
        error=metadata.error if metadata.status in {"failed", "cancelled"} else None,
        isError=metadata.status == "failed",
        awaitingUser=metadata.status == "pending" and not metadata.background,
        display_data=deepcopy(metadata.display_data),
    )
    if metadata.team_run_id is not None:
        fields["team_run_id"] = metadata.team_run_id
    # 只在为真时写入：结算事件不带标记，不能把 part 上已有的标记清掉。
    if metadata.background:
        fields["background"] = True
    display = metadata.display_data
    if display is not None and _refers_to_child_task(display):
        child_task_id = display.get("child_task_id")
        role = display.get("role")
        if (
            isinstance(child_task_id, int)
            and not isinstance(child_task_id, bool)
            and child_task_id > 0
        ):
            fields["child_task_id"] = child_task_id
        if isinstance(role, str) and role.strip():
            fields["agent_role"] = role
    return fields


def _refers_to_child_task(display: dict[str, object]) -> bool:
    kind = display.get("kind")
    if kind not in _CHILD_TASK_KINDS:
        return False
    return kind == "delegation-result" or display.get("operation") == "send"
