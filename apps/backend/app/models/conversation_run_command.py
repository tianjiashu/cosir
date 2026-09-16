"""Conversation Run 创建/编辑使用的领域输入命令。"""

from dataclasses import dataclass, field

from app.models.conversation_run_attachment_input import ConversationRunAttachmentInput


@dataclass(frozen=True, slots=True)
class ConversationRunCommand:
    """描述一次新建或编辑 Run 的已归一化用户输入。

    ``display_text`` 保留用户可见文本和普通附件 token；图片只携带已上传附件 id，
    普通附件携带可选的本机路径。该类型不依赖 Assistant Transport 的 wire schema，
    由 Transport 适配层或其它本地入口构造，交给 ``ConversationRunService`` 解析。
    """

    display_text: str
    image_asset_ids: list[str] = field(default_factory=list)
    attachments: list[ConversationRunAttachmentInput] = field(default_factory=list)


__all__ = ["ConversationRunCommand"]
