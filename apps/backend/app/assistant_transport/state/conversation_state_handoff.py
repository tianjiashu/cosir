"""进行中交接的 Transport 契约：snapshot 顶层 ``handoffs``。

事实源是进程内的交接登记表 ``handoff_registry``（``core/tool/handoff``）：不持久化、不跨后端重启，
所以不从 canonical 记录重建，只在 snapshot 物化与登记表变化通知时整表读取。前端据 ``surface`` 与
``resourceId`` 把交接提示放到对应面板（终端会话、浏览器标签），「交还」即对 ``toolCallId`` 作出 HITL
决定。工具卡片上的交还入口不读这里（卡片状态的事实源是等待占位行），重启后登记表为空，卡片入口仍在。
"""

from typing import get_args

from typing_extensions import TypedDict

from app.assistant_transport.state.wire_shape import (
    as_shape,
    require_list,
    require_non_empty_str,
    require_one_of,
    require_str,
)
from app.core.tool.handoff.handoff_registry import HandoffLease, HandoffSurface


class ConversationHandoff(TypedDict):
    """一次进行中的交接，按登记先后排列。"""

    toolCallId: str
    toolName: str
    surface: HandoffSurface
    resourceId: str
    reason: str


_SURFACES = frozenset(get_args(HandoffSurface))


def project_handoff(lease: HandoffLease) -> ConversationHandoff:
    return ConversationHandoff(
        toolCallId=lease.tool_call_id,
        toolName=lease.tool_name,
        surface=lease.surface,
        resourceId=lease.resource_id,
        reason=lease.reason,
    )


def validate_handoffs(value: object) -> None:
    """精确校验 ``handoffs``：字段集合与取值词表，调用与资源各自唯一。

    同一资源同一时刻至多一次交接。

    异常:
        ValueError: 任一条目不满足契约。
    """

    calls: set[str] = set()
    resources: set[tuple[str, str]] = set()
    for entry in require_list(value, "snapshot handoffs"):
        handoff = as_shape(entry, ConversationHandoff, "snapshot handoff entry")
        tool_call_id = require_non_empty_str(handoff["toolCallId"], "handoff toolCallId")
        resource_id = require_non_empty_str(handoff["resourceId"], "handoff resourceId")
        require_str(handoff["toolName"], "handoff toolName")
        require_str(handoff["reason"], "handoff reason")
        require_one_of(handoff["surface"], _SURFACES, "handoff surface")
        resource = (handoff["surface"], resource_id)
        if tool_call_id in calls or resource in resources:
            raise ValueError("snapshot contains duplicate handoffs")
        calls.add(tool_call_id)
        resources.add(resource)
