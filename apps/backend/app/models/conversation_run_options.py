"""一次 Run 冻结的执行选项，不包含用户消息或附件。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ReasoningEffort = Literal["low", "high", "max"]


class ConversationRunOptions(BaseModel):
    """创建 Run 时确定的工具限制和推理强度，后续入队消息不修改当前 Run 配置。

    ``disabled_tool_groups`` 冻结的是用户关掉的**工具组名**（用户意图本身），不是展开后的工具名：
    前端按组展示与锁定，无需反推；具体禁用哪些工具由 ``RuntimeContext.for_run`` 按注册表现算。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    reasoning_effort: ReasoningEffort | None = None
    disabled_tool_groups: list[str] = Field(default_factory=list)

    @field_validator("disabled_tool_groups")
    @classmethod
    def validate_groups(cls, groups: list[str]) -> list[str]:
        """拒绝空名和重复项，避免授权配置存在歧义；排序后保存，运行期配置锁定按集合比较。"""
        if any(not group for group in groups) or len(groups) != len(set(groups)):
            raise ValueError("disabled_tool_groups must contain unique non-empty names")
        return sorted(groups)
