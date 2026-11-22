"""交付记录的 wire 投影与写入侧校验：工具 part 上的 ``deliverables`` 字段，以及 snapshot 顶层的会话清单。

事实源是结果行 metadata 的 ``facts.deliverables``（``app.models.deliverable``）；这里只把它转成驼峰的 wire 形状，
并在快照写入侧精确校验，前端严格校验是同一份契约。顶层清单是工作副本里全部 part 上记录的纯函数（侧栏「产出」卡读它），
条目与 part 上的同一形状。
"""

from collections.abc import Sequence
from typing import cast, get_args

from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_part import ConversationDeliverable
from app.assistant_transport.state.wire_shape import (
    as_shape,
    require_int,
    require_list,
    require_one_of,
    require_str,
)
from app.models.deliverable_file import Deliverable, DeliveredDirectory, DeliveredFile
from app.models.enums.file_kind import FileKind

_KINDS: frozenset[str] = frozenset(get_args(FileKind))
_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    "file": frozenset({"workspacePath", "absolutePath", "kind", "size", "width", "height"}),
    "directory": frozenset({"workspacePath", "absolutePath"}),
    "url": frozenset({"url"}),
}


def project_deliverables(items: Sequence[Deliverable]) -> list[ConversationDeliverable]:
    """交付记录的 wire 形状；``key`` 由值对象算出，前端去重只比较它。"""

    projected: list[ConversationDeliverable] = []
    for item in items:
        entry = ConversationDeliverable(type=item.type, key=item.key, caption=item.caption)
        if isinstance(item, DeliveredFile | DeliveredDirectory):
            entry["workspacePath"] = item.workspace_path
            entry["absolutePath"] = item.absolute_path
            if isinstance(item, DeliveredFile):
                entry["kind"] = item.kind
                entry["size"] = item.size
                entry["width"] = item.width
                entry["height"] = item.height
        else:
            entry["url"] = item.url
        projected.append(entry)
    return projected


def project_session_deliverables(
    messages: Sequence[ConversationStateMessage],
) -> list[ConversationDeliverable]:
    """会话交付清单：按 ``key`` 去重、留最后一次、最近交付的在前。

    ``messages`` 必须是工作副本里的完整历史：发给前端的完整快照只按窗口裁剪 ``messages``，顶层清单原样带上，
    所以没加载进窗口的早期交付物也在清单里。继承的消息计入（与后台看板相反）：手动 fork 会展示继承的历史与
    卡片，侧栏应一致；Agent fork 的继承行本来就不进 Transport。撤回的回合不在工作副本里，自然不在清单里。
    """

    latest: dict[str, ConversationDeliverable] = {}
    for message in messages:
        for part in message["parts"]:
            if part["type"] != "tool-call":
                continue
            for item in part.get("deliverables", ()):
                latest.pop(item["key"], None)
                latest[item["key"]] = item
    return list(reversed(latest.values()))


def validate_session_deliverables(value: object) -> None:
    """精确校验 snapshot 顶层 ``deliverables``：条目同 part 上的形状，``key`` 不重复。

    异常:
        ValueError: 不满足契约。
    """

    validate_deliverables(value)
    keys = [cast(ConversationDeliverable, item)["key"] for item in cast(list[object], value)]
    if len(keys) != len(set(keys)):
        raise ValueError("snapshot deliverables keys must be unique")


def validate_deliverables(value: object) -> None:
    """精确校验工具 part 上的 ``deliverables``：每项的字段集合随 ``type`` 而定。

    异常:
        ValueError: 不满足契约。
    """

    for raw in require_list(value, "snapshot tool deliverables"):
        item = as_shape(raw, ConversationDeliverable, "snapshot deliverable")
        require_one_of(item["type"], frozenset(_FIELDS_BY_TYPE), "snapshot deliverable type")
        require_str(item["key"], "snapshot deliverable key")
        require_str(item["caption"], "snapshot deliverable caption", nullable=True)
        extra = set(item) - {"type", "key", "caption"}
        if extra != _FIELDS_BY_TYPE[item["type"]]:
            raise ValueError(f"snapshot deliverable fields do not match type {item['type']}")
        if item["type"] == "url":
            require_str(item["url"], "snapshot deliverable url")
            continue
        require_str(item["workspacePath"], "snapshot deliverable workspacePath", nullable=True)
        require_str(item["absolutePath"], "snapshot deliverable absolutePath")
        if item["type"] == "file":
            require_one_of(item["kind"], _KINDS, "snapshot deliverable kind")
            require_int(item["size"], "snapshot deliverable size", minimum=0)
            require_int(item["width"], "snapshot deliverable width", minimum=1, nullable=True)
            require_int(item["height"], "snapshot deliverable height", minimum=1, nullable=True)
