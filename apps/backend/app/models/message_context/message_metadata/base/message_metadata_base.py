"""消息附属信息的公共契约：按 SDK 消息角色选结构，不保存第二份角色或 Run 状态。"""

from pydantic import BaseModel, ConfigDict


class MessageMetadata(BaseModel):
    """一条消息的附属信息基类：各角色用不同子类，未知字段一律失败（``extra="forbid"``）。

    只承载各角色共用的严格性与按角色的解析入口；具体字段归各角色的 metadata（``system_`` /
    ``user_`` / ``assistant_`` / ``tool_`` 开头的模块）。
    """

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def parse(cls, role: str, value: object) -> "MessageMetadata":
        """持久化边界按消息角色校验 JSON；未知字段或角色错配直接失败，不读取旧形状。

        子类在方法内按需导入：它们都继承本类，模块级导入会形成循环。``developer`` / ``function``
        没有附属信息，解析成 :class:`EmptyMessageMetadata`（与本类同模块）。
        """
        from app.models.message_context.message_metadata.assistant_message_metadata import (
            AssistantMessageMetadata,
        )
        from app.models.message_context.message_metadata.system_message_metadata import (
            SystemMessageMetadata,
        )
        from app.models.message_context.message_metadata.tool_message_metadata import (
            ToolMessageMetadata,
        )
        from app.models.message_context.message_metadata.user_message_metadata import (
            UserMessageMetadata,
        )

        metadata_type: type[MessageMetadata]
        if role == "user":
            metadata_type = UserMessageMetadata
        elif role == "assistant":
            metadata_type = AssistantMessageMetadata
        elif role == "tool":
            metadata_type = ToolMessageMetadata
        elif role == "system":
            metadata_type = SystemMessageMetadata
        elif role in {"developer", "function"}:
            metadata_type = EmptyMessageMetadata
        else:
            raise ValueError(f"unsupported message role: {role}")
        return metadata_type.model_validate(value)

    @classmethod
    def empty(cls, role: str) -> "MessageMetadata":
        """创建没有附属信息的消息；工具结果必须由调用方提供明确展示状态。"""
        return cls.parse(role, {})


class EmptyMessageMetadata(MessageMetadata):
    """developer/function 消息没有额外持久化信息。"""
