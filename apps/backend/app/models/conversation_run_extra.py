"""Conversation Run 的可扩展输入附加事实。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.config.constant import Constant
from app.models.conversation_run_file_attachment import ConversationRunFileAttachment

ReasoningEffort = Literal["low", "high", "max"]


@dataclass(frozen=True, slots=True)
class ConversationRunExtra:
    """一次 Conversation Run 的扩展输入事实。

    字段承载 Assistant 用户可见文本、普通本机文件附件引用、本次 Run 禁用的工具名和
    用户显式指定的推理强度，以及 Langfuse 根 Trace ID 等运行期附加观测事实。最终
    ``allows_tools`` 只在运行时根据 Task 固化工具目录
    与 ``ban_tools`` 计算，不作为持久化字段保存。
    该类是内存中的领域值对象；写入 ``conversation_runs.extra`` 时由 ``to_dict`` 转成
    JSON 对象，从数据库读取时由 ``from_dict`` 恢复。它不保存附件二进制，也不负责检查
    路径是否仍然存在或是否属于当前工作区；模型能力由 ``model_config_id`` 指向的模型
    配置统一提供。
    """

    display_text: str
    attachments: list[ConversationRunFileAttachment]
    reasoning_effort: ReasoningEffort | None = None
    ban_tools: list[str] = field(default_factory=list)
    propose_agent_configuration: bool = False
    langfuse_trace_id: str | None = None

    def __post_init__(self) -> None:
        """校验并规范化普通附件引用与展示文本中的 token 关系。"""

        if not isinstance(self.display_text, str):
            raise TypeError("display_text must be a string")
        if not isinstance(self.ban_tools, list) or any(
            not isinstance(name, str) or not name for name in self.ban_tools
        ):
            raise TypeError("ban_tools must be a list of non-empty strings")
        if len(self.ban_tools) != len(set(self.ban_tools)):
            raise ValueError("ban_tools must not contain duplicates")
        object.__setattr__(self, "ban_tools", list(self.ban_tools))
        if not isinstance(self.propose_agent_configuration, bool):
            raise TypeError("propose_agent_configuration must be a boolean")
        if self.langfuse_trace_id is not None and (
            not isinstance(self.langfuse_trace_id, str) or not self.langfuse_trace_id.strip()
        ):
            raise ValueError("langfuse_trace_id must be a non-empty string")
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
        if self.reasoning_effort not in (None, "low", "high", "max"):
            raise ValueError("reasoning_effort must be one of low/high/max")

    def to_dict(self) -> dict[str, object]:
        """转换为 ``conversation_runs.extra`` 使用的 JSON 对象。"""

        value: dict[str, object] = {
            "display_text": self.display_text,
            "attachments": [dict(attachment) for attachment in self.attachments],
            "ban_tools": list(self.ban_tools),
            "reasoning_effort": self.reasoning_effort,
        }
        if self.propose_agent_configuration:
            value["propose_agent_configuration"] = True
        if self.langfuse_trace_id is not None:
            value["langfuse_trace_id"] = self.langfuse_trace_id
        return value

    @classmethod
    def from_dict(cls, value: object) -> ConversationRunExtra | None:
        """从当前数据库 JSON 恢复扩展事实；缺失扩展值返回 None，结构错误直接暴露。"""

        if value is None:
            return None

        if not isinstance(value, dict):
            raise TypeError("run extra must be an object")

        expected = {
            "display_text",
            "attachments",
            "ban_tools",
            "reasoning_effort",
            "propose_agent_configuration",
            "langfuse_trace_id",
        }
        unknown = set(value) - expected
        missing = (expected - {"propose_agent_configuration", "langfuse_trace_id"}) - set(value)
        if unknown or missing:
            raise ValueError(
                f"run extra fields invalid; unknown={sorted(unknown)}, missing={sorted(missing)}"
            )
        display_text = value["display_text"]
        attachments = value["attachments"]
        ban_tools = value["ban_tools"]
        return cls(
            display_text=display_text,
            attachments=attachments,
            ban_tools=ban_tools,
            reasoning_effort=value["reasoning_effort"],
            propose_agent_configuration=value.get("propose_agent_configuration", False),
            langfuse_trace_id=value.get("langfuse_trace_id"),
        )


__all__ = ["ConversationRunExtra", "ReasoningEffort"]
