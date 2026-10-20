"""上下文 working copy：SDK 消息正文与本地身份、恢复元数据分离。"""

from dataclasses import dataclass, field
from uuid import uuid4

from openai.types.chat import ChatCompletionMessageParam

from app.models.message_metadata import EmptyMessageMetadata, MessageMetadata


@dataclass
class ContextEntry:
    message: ChatCompletionMessageParam
    run_id: int | None
    sequence: int
    message_id: str = field(default_factory=lambda: str(uuid4()))
    metadata: MessageMetadata = field(default_factory=EmptyMessageMetadata)
