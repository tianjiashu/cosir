"""Assistant Transport 的 Run 工具禁用集合命令。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class BanToolsPayload(BaseModel):
    """校验本次 Run 由用户禁用的工具名集合。"""

    model_config = ConfigDict(extra="forbid")

    ban_tools: list[StrictStr]

    @field_validator("ban_tools")
    @classmethod
    def validate_unique_names(cls, value: list[str]) -> list[str]:
        """拒绝空工具名和重复工具名，保持 payload 的确定性。"""

        if any(not name.strip() for name in value):
            raise ValueError("ban_tools entries must not be blank")
        if len(value) != len(set(value)):
            raise ValueError("ban_tools entries must be unique")
        return value


class BanToolsCommand(BaseModel):
    """为单个新 Run 传递用户禁用的工具名集合。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["custom"]
    commandId: str = Field(min_length=1, max_length=128)
    name: Literal["ban-tools"]
    payload: BanToolsPayload


__all__ = ["BanToolsCommand", "BanToolsPayload"]
