"""终端 deny-list 更新请求。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.core.tools.policy.terminal_denylist_contract import (
    MAX_TERMINAL_DENY_PATTERN_LENGTH,
    MAX_TERMINAL_DENY_PATTERNS,
)

TerminalDenyPattern = Annotated[
    str,
    Field(strict=True, max_length=MAX_TERMINAL_DENY_PATTERN_LENGTH),
]


class TerminalDenylistUpdateRequest(BaseModel):
    """只接受有限长度的正则字符串列表。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    patterns: list[TerminalDenyPattern] = Field(
        max_length=MAX_TERMINAL_DENY_PATTERNS,
    )
