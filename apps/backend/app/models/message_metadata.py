"""消息正文之外的持久化契约；由 SDK 消息角色选择结构，不保存第二份角色或 Run 状态。"""

from typing import Literal

from openai.types import CompletionUsage
from pydantic import BaseModel, ConfigDict, model_validator

from app.models.conversation_input import UserInputPresentation
from app.models.enums.tool_call_status import ToolCallEventStatus


class EmptyMessageMetadata(BaseModel):
    """system/developer/function 消息没有额外持久化信息。"""

    model_config = ConfigDict(extra="forbid")


MessageKind = Literal[
    "user_input", "notice", "decision", "agent_result", "background_result", "tool_result"
]
"""Task 消息队列的消息种类；并入 context 后记在 user 消息的 metadata 上，后端规则只读它。"""


class UserMessageMetadata(BaseModel):
    """落库的 user 消息都来自 Task 消息队列：``kind`` 记录来源，用户输入另带展示绑定。

    所有队列消息进模型时都是 user 角色，区分来源只能靠 ``kind``，不解析正文标签：编辑资格、
    排队与消费投影都据此判断。模型正文经附件解析后有损（附件名、类型、图片位置与编辑器 token
    顺序都不在其中），snapshot 重建、编辑复用附件与 fork 都只能从 ``presentation`` 取。内部注入
    的 user 指令（如结构化输出）只拼进请求副本、不落库。
    """

    model_config = ConfigDict(extra="forbid")
    kind: MessageKind
    presentation: UserInputPresentation | None = None

    @model_validator(mode="after")
    def _presentation_only_for_user_input(self) -> "UserMessageMetadata":
        if (self.kind == "user_input") != (self.presentation is not None):
            raise ValueError("presentation is required for user_input and forbidden otherwise")
        return self


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
