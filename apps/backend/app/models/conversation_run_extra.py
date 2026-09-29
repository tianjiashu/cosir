"""Conversation Run 的可扩展输入附加事实。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config.constant import Constant
from app.models.conversation_run_file_attachment import ConversationRunFileAttachment
from app.models.conversation_run_model_snapshot import ConversationRunModelSnapshot


@dataclass(frozen=True, slots=True)
class ConversationRunExtra:
    """一次 Conversation Run 的扩展输入事实。

    字段承载 Assistant 用户可见文本、普通本机文件附件引用、本次 Run 禁用的工具名和
    模型执行快照。
    该类是内存中的领域值对象；写入 ``conversation_runs.extra`` 时由 ``to_dict`` 转成
    JSON 对象，从数据库读取时由 ``from_dict`` 恢复。它不保存附件二进制，也不负责检查
    路径是否仍然存在或是否属于当前工作区。
    """

    display_text: str
    attachments: list[ConversationRunFileAttachment]
    model_snapshot: ConversationRunModelSnapshot
    ban_tools: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """校验并规范化普通附件引用与展示文本中的 token 关系。"""

        if not isinstance(self.display_text, str):
            raise TypeError("display_text must be a string")
        banned = self.ban_tools
        if not isinstance(banned, list) or any(
            not isinstance(name, str) or not name for name in banned
        ):
            raise TypeError("ban_tools must be a list of non-empty strings")
        if len(banned) != len(set(banned)):
            raise ValueError("ban_tools must not contain duplicates")
        object.__setattr__(self, "ban_tools", list(banned))
        if len(self.attachments) > 32:
            raise ValueError("attachments must contain at most 32 items")

        normalized: list[ConversationRunFileAttachment] = []
        seen_ids: set[str] = set()
        for attachment in self.attachments:
            if not isinstance(attachment, dict):
                raise TypeError("attachment must be a mapping")
            if not all(
                isinstance(attachment.get(key), str)
                for key in ("id", "name", "content_type", "path")
            ):
                raise TypeError("attachment fields must be strings")
            attachment_id = attachment["id"]
            if (
                not Constant.Cosir.LOCAL_FILE_ID.fullmatch(attachment_id)
                or not attachment["name"]
                or not attachment["content_type"]
                or not attachment["path"].strip()
                or attachment_id in seen_ids
            ):
                raise ValueError("attachment fields are invalid")
            seen_ids.add(attachment_id)
            normalized.append(
                {
                    "id": attachment_id,
                    "name": attachment["name"],
                    "content_type": attachment["content_type"],
                    "path": attachment["path"],
                }
            )

        token_ids = list(dict.fromkeys(Constant.Cosir.LOCAL_FILE_TOKEN.findall(self.display_text)))
        if token_ids != [attachment["id"] for attachment in normalized]:
            raise ValueError("display_text attachment tokens do not match attachments")
        object.__setattr__(self, "attachments", normalized)
        if not isinstance(self.model_snapshot, ConversationRunModelSnapshot):
            raise TypeError("model_snapshot must be a ConversationRunModelSnapshot")

    def to_dict(self) -> dict[str, object]:
        """转换为 ``conversation_runs.extra`` 使用的 JSON 对象。"""

        return {
            "display_text": self.display_text,
            "attachments": [dict(attachment) for attachment in self.attachments],
            "ban_tools": list(self.ban_tools),
            "model_snapshot": self.model_snapshot.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> ConversationRunExtra | None:
        """从当前数据库 JSON 恢复扩展事实；缺失扩展值返回 None，结构错误直接暴露。"""

        if value is None:
            return None

        if not isinstance(value, dict):
            raise TypeError("run extra must be an object")

        expected = {"display_text", "attachments", "ban_tools", "model_snapshot"}
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown or missing:
            raise ValueError(
                f"run extra fields invalid; unknown={sorted(unknown)}, missing={sorted(missing)}"
            )
        display_text = value["display_text"]
        attachments = value["attachments"]
        ban_tools = value["ban_tools"]
        model_snapshot_value = value["model_snapshot"]
        return cls(
            display_text=display_text,
            attachments=attachments,
            ban_tools=ban_tools,
            model_snapshot=ConversationRunModelSnapshot.from_dict(model_snapshot_value),
        )


__all__ = ["ConversationRunExtra"]
