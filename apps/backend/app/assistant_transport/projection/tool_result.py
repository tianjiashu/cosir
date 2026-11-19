"""工具 metadata 到卡片字段的唯一转换，不读取正文、注册表或业务数据库。"""

from copy import deepcopy
from typing import NotRequired, TypedDict

from app.assistant_transport.state.conversation_state_background_task import (
    project_background_work,
)
from app.assistant_transport.state.conversation_state_deliverable import project_deliverables
from app.assistant_transport.state.conversation_state_part import (
    ConversationBackgroundWork,
    ConversationDeliverable,
)
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.models.message_metadata import ToolMessageMetadata


class ToolResultFields(TypedDict):
    status: ToolCallEventStatus
    error: str | None
    awaitingUser: bool
    display_data: dict[str, object] | None
    background: NotRequired[ConversationBackgroundWork]
    deliverables: NotRequired[list[ConversationDeliverable]]


def project_tool_result(metadata: ToolMessageMetadata) -> ToolResultFields:
    """实时状态与冷重建采用同一错误口径和关联信息，返回副本避免污染持久事实。

    ``awaitingUser`` 按 metadata 计算：占位行 ``pending`` 且不是后台占位，就是「等你操作」（HITL 等
    用户决定）。每次都显式写，决定到达时自然清回 ``False``。实时投影的行
    恒属当前 Run、在上下文里；冷重建另按行级规则 ``SessionContextRecord.awaits_user``
    纠正继承副本与撤回移出的行。
    """
    fields = ToolResultFields(
        status=metadata.status,
        error=metadata.error if metadata.status in {"failed", "cancelled"} else None,
        awaitingUser=metadata.awaiting_decision,
        display_data=deepcopy(metadata.display_data),
    )
    # 只在有记录时写入：不带记录的事件不能把 part 上已有的记录清掉。
    if metadata.background is not None:
        fields["background"] = project_background_work(metadata.background)
    if metadata.deliverables is not None:
        fields["deliverables"] = project_deliverables(metadata.deliverables)
    return fields
