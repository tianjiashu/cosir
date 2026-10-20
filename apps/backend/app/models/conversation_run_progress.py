"""Run 持久进度契约；只保存继续执行所需事实，不承载生命周期状态。"""

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.models.user_decision import UserDecision


class ToolContinuation(BaseModel):
    """等待时冻结唯一待决调用及观察；决定提交后供恢复入口一次性消费。"""

    model_config = ConfigDict(extra="forbid")
    observations: list[dict[str, JsonValue]]
    pending_call: ChatCompletionMessageFunctionToolCallParam
    decision: UserDecision | None = None


class ConversationRunProgress(BaseModel):
    """跨驱动保留步骤号与错误计数；等待完成、编辑或恢复收口时清除等待数据。"""

    model_config = ConfigDict(extra="forbid")
    step_count: int = Field(default=0, ge=0)
    tool_error_count: int = Field(default=0, ge=0)
    continuation: ToolContinuation | None = None
