"""交互终端工具的输入契约（6 个工具：start / write / read / handoff / list / close）。

参数模型不写类 docstring：它会作为顶层 description 进入模型可见 schema，说明改写成注释。
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.constant import Constant
from app.tools.terminal_runtime.inputs import (
    InputKeys,
    InputLine,
    InputSignal,
    InputText,
    TerminalWait,
)

_SESSION_ID_DESCRIPTION = "Session ID from terminal_start or terminal_list."


# 会话归属取自执行上下文，不允许模型指定其他 Task。
class TerminalListArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class TerminalStartArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    shell: str = Field(
        default="auto",
        min_length=1,
        max_length=256,
        # 模型可见描述由 terminal_start 按宿主改写（补 auto 的解析结果），此处只是本地兜底。
        description="'auto' uses the host default shell; terminal_list shows the others.",
    )
    label: str | None = Field(
        default=None,
        max_length=64,
        description="Short purpose shown on the tab and in terminal_list, e.g. 'dev server'.",
    )
    cwd: str | None = Field(
        default=None,
        description="Start directory inside the workspace; relative paths resolve from the "
        "workspace root (the default). It must exist.",
    )
    integration: Literal["auto", "off"] = Field(
        default="auto",
        description="auto enables shell integration (command boundaries, exit codes, cwd "
        "tracking) where the shell supports it; off disables it.",
    )


class TerminalWriteArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, description=_SESSION_ID_DESCRIPTION)
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Idempotency key: retrying with the same key and identical input is not sent twice. "
            "Use a new key for each new write (the last "
            f"{Constant.Terminal.MAX_WRITE_OPERATION_CACHE} are remembered)."
        ),
    )
    # 普通 union 导出 strict 支持的 anyOf，不把判别联合的 discriminator/oneOf 泄漏到模型 schema。
    input: InputLine | InputText | InputKeys | InputSignal = Field(
        # 各输入类型的语义由各分支字段描述承载，这里只点明「每次一个动作」。
        description="Exactly one input action: line, text, keys, or signal.",
    )
    wait: TerminalWait | None = Field(
        default=None,
        description=(
            "When to return after the input; null means until=output_quiet with default "
            "quiet_ms and timeout_ms."
        ),
    )


# 按行读取；带 wait 时先按条件等待再读。
class TerminalReadArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, description=_SESSION_ID_DESCRIPTION)
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


# 交接（HITL）：只是会话上的提示，不写任何字节；用户交还即对这次调用作出决定。
class TerminalHandoffArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, description=_SESSION_ID_DESCRIPTION)
    reason: str = Field(
        min_length=1,
        max_length=200,
        description="One sentence telling the user what to do, e.g. 'Enter the sudo password'.",
    )


class TerminalCloseArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, description=_SESSION_ID_DESCRIPTION)
