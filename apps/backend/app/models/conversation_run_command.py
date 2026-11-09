"""Conversation Run 创建使用的领域输入命令。"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ConversationRunCommand:
    """描述首条或追加消息的已归一化用户输入。

    ``text`` 是编辑器产出的胶囊正文（``<file>`` / ``<ref>`` / ``<skill>`` 指针）；图片只携带已上传
    附件 id，``disabled_tool_groups`` 保留用户本次 Run 关掉的工具组名。组名到工具名的展开与
    ``admitted_tools`` 都不在此命令中提前计算，而是在运行时结合注册表与 Task 固化工具目录生成。
    该类型不依赖 Assistant Transport 的 wire schema，由 Transport 适配层或其它本地入口构造，
    经输入准备服务校验后存入 message_queue；模型边界消费时归属 Run 和 context。
    """

    text: str
    image_asset_ids: list[str] = field(default_factory=list)
    disabled_tool_groups: list[str] = field(default_factory=list)


__all__ = ["ConversationRunCommand"]
