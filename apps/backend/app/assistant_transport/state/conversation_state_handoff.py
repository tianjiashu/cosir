"""进行中交接的 Transport 契约：snapshot 顶层 ``handoffs``。

事实源是进程内的交接登记表 ``handoff_registry``（``core/tool/handoff``）：不持久化、不跨后端重启，
所以不从 canonical 记录重建，只在 snapshot 物化与登记表变化通知时整表读取。前端据 ``surface`` 与
``resourceId`` 把交接提示放到对应面板（终端会话、浏览器标签），「交还」即对 ``toolCallId`` 作出 HITL
决定。工具卡片上的交还入口不读这里（卡片状态的事实源是等待占位行），重启后登记表为空，卡片入口仍在。
"""

from typing import Literal

from typing_extensions import TypedDict

from app.core.tool.handoff.handoff_registry import HandoffLease


class ConversationHandoff(TypedDict):
    """一次进行中的交接，按登记先后排列。"""

    toolCallId: str
    toolName: str
    surface: Literal["terminal", "browser"]
    resourceId: str
    reason: str


HANDOFF_KEYS = frozenset(ConversationHandoff.__annotations__)
_SURFACES = frozenset({"terminal", "browser"})


def project_handoff(lease: HandoffLease) -> ConversationHandoff:
    return {
        "toolCallId": lease.tool_call_id,
        "toolName": lease.tool_name,
        "surface": lease.surface,
        "resourceId": lease.resource_id,
        "reason": lease.reason,
    }


def validate_handoffs(value: object) -> None:
    """精确校验 ``handoffs``：键集合、取值枚举，调用与资源各自唯一（同一资源同一时刻至多一次交接）。

    异常:
        ValueError: 任一条目不满足契约。
    """

    if not isinstance(value, list):
        raise ValueError("snapshot handoffs must be an array")
    calls: set[str] = set()
    resources: set[tuple[str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != HANDOFF_KEYS:
            raise ValueError("snapshot handoff entry is invalid")
        if any(not isinstance(entry[key], str) for key in HANDOFF_KEYS):
            raise ValueError("handoff fields must be strings")
        if not entry["toolCallId"] or not entry["resourceId"]:
            raise ValueError("handoff toolCallId and resourceId must be non-empty")
        if entry["surface"] not in _SURFACES:
            raise ValueError("handoff surface is invalid")
        resource = (entry["surface"], entry["resourceId"])
        if entry["toolCallId"] in calls or resource in resources:
            raise ValueError("snapshot contains duplicate handoffs")
        calls.add(entry["toolCallId"])
        resources.add(resource)
