"""Run 持久进度契约；只保存继续执行所需事实，不承载生命周期状态。"""

from pydantic import BaseModel, ConfigDict, Field


class ConversationRunProgress(BaseModel):
    """跨驱动保留步骤号与错误计数。

    不冻结任何工具调用：工具调用在产生它的那一步闭合，挂起时 context 中没有未闭合调用，唤醒、
    续跑都从模型边界继续。
    """

    model_config = ConfigDict(extra="forbid")
    step_count: int = Field(default=0, ge=0)
    tool_error_count: int = Field(default=0, ge=0)
