"""Conversation Run 的可扩展输入附加事实。"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.constant import Constant
from app.models.conversation_run_file_attachment import ConversationRunFileAttachment


@dataclass(frozen=True, slots=True)
class ConversationRunExtra:
    """一次 Conversation Run 的扩展输入事实。

    当前字段承载 Assistant 用户可见文本和普通本机文件附件引用。该类是内存中的
    领域值对象；写入 ``conversation_runs.extra`` 时由 ``to_dict`` 转成 JSON 对象，
    从数据库读取时由 ``from_dict`` 恢复。它不保存附件二进制，也不负责检查路径是否
    仍然存在或是否属于当前工作区。
    """

    display_text: str
    attachments: list[ConversationRunFileAttachment]

    def __post_init__(self) -> None:
        """校验并规范化普通附件引用与展示文本中的 token 关系。"""

        if not isinstance(self.display_text, str):
            raise TypeError("display_text must be a string")
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

    def to_dict(self) -> dict[str, object]:
        """转换为 ``conversation_runs.extra`` 使用的 JSON 对象。"""

        return {
            "display_text": self.display_text,
            "attachments": [dict(attachment) for attachment in self.attachments],
        }

    @classmethod
    def from_dict(cls, value: object) -> ConversationRunExtra | None:
        """从数据库 JSON 恢复扩展事实；非法或未知旧结构返回 ``None``。

        当前格式为顶层 ``display_text`` / ``attachments``。为避免已有工作树或历史
        数据丢失，读取阶段兼容旧的 ``assistant_input`` 包装和 ``version=1``；新写入
        永远不再输出这两个旧字段。
        """

        if not isinstance(value, dict):
            return None

        candidate: object = value
        legacy = value.get("assistant_input")
        if legacy is not None:
            if not isinstance(legacy, dict) or legacy.get("version") != 1:
                return None
            candidate = legacy
        if not isinstance(candidate, dict):
            return None

        display_text = candidate.get("display_text")
        attachments = candidate.get("attachments")
        if not isinstance(display_text, str) or not isinstance(attachments, list):
            return None
        try:
            return cls(display_text=display_text, attachments=attachments)
        except (TypeError, ValueError):
            return None


__all__ = ["ConversationRunExtra"]
