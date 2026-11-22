"""终端权限的持久化模式与单次能力申请；不改变文件工具权限。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TerminalPermissionMode = Literal["request_approval", "full_access"]


class TerminalPermissions(BaseModel):
    """申请只对本条命令有效；模型不能通过它关闭沙箱。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    network: bool = Field(
        default=False,
        description=(
            "Allow IP network access for this command and its descendants, "
            "including localhost and listening ports."
        ),
    )
    write_roots: list[str] = Field(
        default_factory=list,
        max_length=16,
        description=(
            "Existing directories that this command must write outside its base writable root. "
            "Cosir control directories stay protected."
        ),
    )
