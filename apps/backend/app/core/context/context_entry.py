"""上下文 working copy：SDK 消息正文与本地身份、恢复元数据分离。"""

from dataclasses import dataclass, field

from openai.types.chat import ChatCompletionMessageParam

from app.models.message_metadata import EmptyMessageMetadata, MessageMetadata


@dataclass
class ContextEntry:
    message: ChatCompletionMessageParam
    run_id: int | None
    sequence: int
    # 只有用户消息有身份（inbox 行 id，排队与已消费阶段共用）；其余角色为 None。
    message_id: int | None = None
    metadata: MessageMetadata = field(default_factory=EmptyMessageMetadata)
