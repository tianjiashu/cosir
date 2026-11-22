"""单次模型响应的观测信息。"""

from openai.types import CompletionUsage

from app.models.message_context.message_metadata.base.message_metadata_base import MessageMetadata


class AssistantMessageMetadata(MessageMetadata):
    """单次模型响应的观测信息，不进入后续模型请求；完成原因保留 Provider 原值。"""

    completion_id: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    usage: CompletionUsage | None = None
