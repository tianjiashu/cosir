"""terminal_read 工具的参数契约。"""

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.constant import Constant
from app.tools.terminal_runtime.observation.wait import TerminalWait

_TERMINAL_ID_DESCRIPTION = (
    "Terminal ID from terminal_list: a terminal the user opened (term_…), or the terminal_id of "
    "your command still running from execute_terminal."
)


class TerminalReadArgs(BaseModel):
    """按行读取终端输出；带 ``wait`` 时先按条件等待再读。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    terminal_id: str = Field(min_length=1, max_length=128, description=_TERMINAL_ID_DESCRIPTION)
    start_line: int | None = Field(
        default=None,
        ge=1,
        description="Absolute line number to read forward from (with limit); null reads the tail.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=Constant.Terminal.READ_MAX_LIMIT,
        description="Lines to read from start_line.",
    )
    tail: int | None = Field(
        default=None,
        ge=1,
        le=Constant.Terminal.READ_MAX_LIMIT,
        description=(
            f"Last N lines when start_line is null (default {Constant.Terminal.READ_DEFAULT_TAIL})."
        ),
    )
    pattern: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description=(
            "Python regex: return only matching lines, searching forward from start_line "
            "(default: the oldest kept line) to the end; limit caps the matches. Use it to find "
            "errors in long output instead of paging. Not combined with tail."
        ),
    )
    wait: TerminalWait | None = Field(
        default=None,
        description="Wait for this condition before reading; null reads now.",
    )

    @model_validator(mode="after")
    def validate_pattern(self) -> "TerminalReadArgs":
        if self.pattern is None:
            return self
        if self.tail is not None:
            raise ValueError("pattern searches forward from start_line; do not combine with tail")
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"invalid pattern: {exc}") from exc
        return self
