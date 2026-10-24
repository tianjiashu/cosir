"""一次 Run 冻结的执行选项，不包含用户消息或附件。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ReasoningEffort = Literal["low", "high", "max"]


class ConversationRunOptions(BaseModel):
    """创建 Run 时确定的工具限制和推理强度，后续入队消息不修改当前 Run 配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    reasoning_effort: ReasoningEffort | None = None
    ban_tools: list[str] = Field(default_factory=list)

    @field_validator("ban_tools")
    @classmethod
    def validate_tools(cls, names: list[str]) -> list[str]:
        """拒绝空标识和重复项，避免授权配置存在歧义；排序后保存，运行期配置锁定按集合比较。"""
        if any(not name for name in names) or len(names) != len(set(names)):
            raise ValueError("ban_tools must contain unique non-empty names")
        return sorted(names)
