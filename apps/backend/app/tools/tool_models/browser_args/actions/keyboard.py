"""键盘动作：往输入框里打字、按键或按快捷键。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tools.tool_models.browser_args._shared import MODIFIERS, REF_FIELD, REF_PATTERN

_KEY_BY_KEY_MAX_CHARS = 500

_NAMED_KEY = Literal[
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Space",
]
# 单个小写字母或数字：配合修饰键组成快捷键（大写用 Shift）。
_CHARACTER_KEY = Annotated[str, Field(pattern=r"^[a-z0-9]$")]


class TypeAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["type"]
    ref: str = REF_FIELD
    text: str = Field(max_length=10_000, description="Text to type into the field.")
    clear: bool = Field(default=False, description="Clear the field first.")
    submit: bool = Field(default=False, description="Press Enter after typing.")
    key_by_key: bool = Field(
        default=False,
        description=(
            "Type one key at a time with key events instead of inserting the text at once, for "
            "fields that react to each keystroke (autocomplete, masked or code inputs). Slower; "
            f"text up to {_KEY_BY_KEY_MAX_CHARS} characters."
        ),
    )

    # 逐键输入每个字符两条 CDP 命令：限长让一次动作的耗时可预期（宿主时限按动作数估算）。
    @model_validator(mode="after")
    def _key_by_key_length(self) -> "TypeAction":
        if self.key_by_key and len(self.text) > _KEY_BY_KEY_MAX_CHARS:
            raise ValueError(f"key_by_key takes at most {_KEY_BY_KEY_MAX_CHARS} characters")
        return self


class PressAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["press"]
    key: _NAMED_KEY | _CHARACTER_KEY = Field(
        description="A named key, or a single lowercase letter or digit for shortcuts."
    )
    modifiers: MODIFIERS | None = Field(
        default=None,
        max_length=4,
        description=(
            "Keys held down while pressing, for shortcuts the page handles (e.g. ControlOrMeta "
            "with Enter to send). ControlOrMeta is Cmd on macOS and Ctrl elsewhere. Editing "
            "shortcuts such as select-all or undo are not supported; edit text with type."
        ),
    )
    ref: str | None = Field(
        default=None,
        pattern=REF_PATTERN,
        description="Focus this element before pressing; null presses on the focused element.",
    )
