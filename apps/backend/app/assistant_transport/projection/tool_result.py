"""工具 metadata 到卡片字段的唯一转换，不读取正文、注册表或业务数据库。"""

from copy import deepcopy
from typing import NotRequired, TypedDict

from app.assistant_transport.state.conversation_state_background_task import (
    project_background_work,
)
from app.assistant_transport.state.conversation_state_part import ConversationBackgroundWork
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.models.message_metadata import ToolMessageMetadata


class ToolResultFields(TypedDict):
    status: ToolCallEventStatus
    error: str | None
    isError: bool
    awaitingUser: bool
    display_data: dict[str, object] | None
    background: NotRequired[ConversationBackgroundWork]
    child_task_id: NotRequired[int]
    agent_role: NotRequired[str]


def project_tool_result(metadata: ToolMessageMetadata) -> ToolResultFields:
    """实时状态与冷重建采用同一错误口径和关联信息，返回副本避免污染持久事实。

    ``awaitingUser`` 按 metadata 计算：占位行 ``pending`` 且不是后台占位，就是「等你操作」（HITL 等
    决定、交接等交还）。每次都显式写，决定到达、重放写回终态时自然清回 ``False``。实时投影的行
    恒属当前 Run、在上下文里；冷重建另按行级规则 ``ConversationTaskContextRecord.awaits_user``
    纠正继承副本与撤回移出的行。
    """
    fields = ToolResultFields(
        status=metadata.status,
        error=metadata.error if metadata.status in {"failed", "cancelled"} else None,
        isError=metadata.status == "failed",
        awaitingUser=metadata.awaiting_decision,
        display_data=deepcopy(metadata.display_data),
    )
    # 只在有记录时写入：不带记录的事件不能把 part 上已有的记录清掉。
    if metadata.background is not None:
        fields["background"] = project_background_work(metadata.background)
    display = metadata.display_data
    if display is not None and display.get("kind") in {"delegation-result", "fork-task-result"}:
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
