"""Conversation Run 创建/编辑使用的领域输入命令。"""

from dataclasses import dataclass, field

from app.models.conversation_run_attachment_input import ConversationRunAttachmentInput


@dataclass(frozen=True, slots=True)
class ConversationRunCommand:
    """描述一次新建或编辑 Run 的已归一化用户输入。

    ``display_text`` 保留用户可见文本和普通附件 token；图片只携带已上传附件 id，
    普通附件携带可选的本机路径，``ban_tools`` 保留用户本次 Run 禁用的工具名列表；配置草稿
    开关只开放对应提案工具，不携带保存或执行指令。
    ``allows_tools`` 不在此命令中提前计算，而是在运行时结合 Task 固化工具目录后生成
    最终执行快照。该类型不依赖 Assistant Transport 的 wire schema，由 Transport 适配层
    或其它本地入口构造，交给 ``ConversationRunService`` 持久化，再由 Runner 解析。
    """

    display_text: str
    image_asset_ids: list[str] = field(default_factory=list)
    attachments: list[ConversationRunAttachmentInput] = field(default_factory=list)
    ban_tools: list[str] = field(default_factory=list)
    propose_agent_configuration: bool = False
    propose_agent_team_configuration: bool = False


__all__ = ["ConversationRunCommand"]
