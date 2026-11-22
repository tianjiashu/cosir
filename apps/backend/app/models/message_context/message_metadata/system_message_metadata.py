"""Session 固定系统提示词的附属信息，以及 Agent 已看到的配置基准。"""

from pydantic import BaseModel, ConfigDict

from app.models.message_context.message_metadata.base.message_metadata_base import MessageMetadata


class PromptSourceEntry(BaseModel):
    """Agent 已看到的一个配置条目：指纹判断变没变，原文用来算差异（配置变化通知只发 diff）。

    原文只存在 metadata 里、不进模型请求；指纹不一定由原文算出（Skill 条目原文是名称与描述，指纹含正文）。
    """

    model_config = ConfigDict(extra="forbid")
    fingerprint: str
    text: str


PromptSourceBaseline = dict[str, dict[str, PromptSourceEntry]]
"""Agent 已看到的配置：来源键 → 条目键 → 条目（见 ``core.context.prompt_sources``）。"""


class SystemMessageMetadata(MessageMetadata):
    """Session 固定系统提示词（seq 0）的附属信息：生成它时各配置来源的指纹。

    提示词创建后不再改写，这份指纹就是「Agent 一开始看到的配置」，是之后判断配置变化的初始基准；
    分叉原样复制这一行，基准随之继承。
    """

    prompt_sources: PromptSourceBaseline
