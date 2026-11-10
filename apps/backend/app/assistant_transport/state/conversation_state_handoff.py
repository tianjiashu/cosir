"""进行中交接的 Transport 契约：snapshot 顶层 ``handoffs``。

事实源是进程内的交接登记表 ``handoff_registry``（``core/tool/handoff``）：不持久化、不跨后端重启，
所以不从 canonical 记录重建，只在 snapshot 物化与登记表变化通知时整表读取。前端据 ``resourceId``
（浏览器标签 id）把交接提示放到浏览器面板，「交还」即对 ``toolCallId`` 作出 HITL 决定。工具卡片上的
交还入口不读这里（卡片状态的事实源是等待占位行），重启后登记表为空，卡片入口仍在。
"""

from typing_extensions import TypedDict

from app.assistant_transport.state.wire_shape import (
    as_shape,
    require_list,
    require_non_empty_str,
    require_str,
)
from app.core.tool.handoff.handoff_registry import HandoffLease


class ConversationHandoff(TypedDict):
    """一次进行中的交接，按登记先后排列。"""

    toolCallId: str
    toolName: str
    resourceId: str
    reason: str


def project_handoff(lease: HandoffLease) -> ConversationHandoff:
    return ConversationHandoff(
        toolCallId=lease.tool_call_id,
        toolName=lease.tool_name,
        resourceId=lease.resource_id,
        reason=lease.reason,
    )


def validate_handoffs(value: object) -> None:
    """精确校验 ``handoffs``：字段集合，调用与资源各自唯一。

    同一资源同一时刻至多一次交接。

    异常:
        ValueError: 任一条目不满足契约。
    """

    calls: set[str] = set()
    resources: set[str] = set()
    for entry in require_list(value, "snapshot handoffs"):
        handoff = as_shape(entry, ConversationHandoff, "snapshot handoff entry")
        tool_call_id = require_non_empty_str(handoff["toolCallId"], "handoff toolCallId")
        resource_id = require_non_empty_str(handoff["resourceId"], "handoff resourceId")
        require_str(handoff["toolName"], "handoff toolName")
        require_str(handoff["reason"], "handoff reason")
        if tool_call_id in calls or resource_id in resources:
            raise ValueError("snapshot contains duplicate handoffs")
        calls.add(tool_call_id)
        resources.add(resource_id)
