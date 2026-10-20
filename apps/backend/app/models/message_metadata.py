"""消息正文之外的持久化契约；由 SDK 消息角色选择结构，不保存第二份角色或 Run 状态。"""

from openai.types import CompletionUsage
from pydantic import BaseModel, ConfigDict

from app.models.conversation_input import UserInputPresentation
from app.models.enums.tool_call_status import ToolCallEventStatus


class EmptyMessageMetadata(BaseModel):
    """system/developer/function 消息没有额外持久化信息。"""

    model_config = ConfigDict(extra="forbid")


class UserMessageMetadata(BaseModel):
    """真实用户输入保留展示绑定；内部注入的 user 消息没有编辑器绑定。"""

    model_config = ConfigDict(extra="forbid")
    presentation: UserInputPresentation | None = None


class AssistantMessageMetadata(BaseModel):
    """单次模型响应的观测信息，不进入后续模型请求；完成原因保留 Provider 原值。"""

    model_config = ConfigDict(extra="forbid")
    completion_id: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    usage: CompletionUsage | None = None


class ToolMessageMetadata(BaseModel):
    """工具结果的展示事实；等待决定归 Run，这里只保留卡片重建所需载荷。"""

    model_config = ConfigDict(extra="forbid")
    status: ToolCallEventStatus
    display_data: dict[str, object] | None = None
    error: str | None = None
    team_run_id: int | None = None
    hidden: bool = False


MessageMetadata = (
    EmptyMessageMetadata | UserMessageMetadata | AssistantMessageMetadata | ToolMessageMetadata
)


def parse_message_metadata(role: str, value: object) -> MessageMetadata:
    """持久化边界按消息角色校验 JSON；未知字段或角色错配直接失败，不读取旧形状。"""
    metadata_type: type[MessageMetadata]
    if role == "user":
        metadata_type = UserMessageMetadata
    elif role == "assistant":
        metadata_type = AssistantMessageMetadata
    elif role == "tool":
        metadata_type = ToolMessageMetadata
    elif role in {"system", "developer", "function"}:
        metadata_type = EmptyMessageMetadata
    else:
        raise ValueError(f"unsupported message role: {role}")
    return metadata_type.model_validate(value)


def empty_message_metadata(role: str) -> MessageMetadata:
    """创建没有附属信息的消息；工具结果必须由调用方提供明确展示状态。"""
    return parse_message_metadata(role, {})
