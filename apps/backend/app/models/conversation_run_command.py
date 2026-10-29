"""Conversation Run 创建/编辑使用的领域输入命令。"""

from dataclasses import dataclass, field

from app.models.conversation_run_attachment_input import ConversationRunAttachmentInput


@dataclass(frozen=True, slots=True)
class ConversationRunCommand:
    """描述首条、追加或编辑消息的已归一化用户输入。

    ``display_text`` 保留用户可见文本和普通附件 token；图片只携带已上传附件 id，
    普通附件携带可选的本机路径，``disabled_tool_groups`` 保留用户本次 Run 关掉的工具组名。
    组名到工具名的展开与 ``allows_tools`` 都不在此命令中提前计算，而是在运行时结合注册表与
    Task 固化工具目录生成。该类型不依赖 Assistant Transport 的 wire schema，由 Transport 适配层
    或其它本地入口构造，经输入准备服务解析后存入 message_queue；模型边界消费时归属 Run 和 context。
    """

    display_text: str
    image_asset_ids: list[str] = field(default_factory=list)
    attachments: list[ConversationRunAttachmentInput] = field(default_factory=list)
    disabled_tool_groups: list[str] = field(default_factory=list)


__all__ = ["ConversationRunCommand"]
