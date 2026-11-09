"""模型更新请求。"""

from pydantic import BaseModel, Field


class ModelConfigUpdateRequest(BaseModel):
    """只覆盖显式传入的上下文窗口与能力声明；模型名不可改，换模型就新增再删除。"""

    context_window_k: int | None = Field(default=None, gt=0)
    supports_thinking: bool | None = None
    supports_reasoning_effort: bool | None = None
    supports_image: bool | None = None
