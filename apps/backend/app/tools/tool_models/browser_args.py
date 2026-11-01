"""内置浏览器工具的输入契约。

参数模型不写类 docstring：它会作为顶层 description 进入模型可见 schema，说明改写成注释。
标签归属取自执行上下文，模型不能指定别的 Task；``tab`` 只在本 Task 的标签之间选择。
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TAB_DESCRIPTION = (
    "Tab id such as t1, from an earlier browser result. Pass null to use this task's current tab."
)


# url 与 action 二选一：打开地址，或在当前标签里后退 / 前进 / 刷新。
class BrowserNavigateArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    url: str | None = Field(
        default=None,
        max_length=4096,
        description="Absolute http(s) URL to open. Leave null when using action.",
    )
    action: Literal["back", "forward", "reload"] | None = Field(
        default=None,
        description=(
            "History action on the tab instead of opening a URL. Leave null when using url."
        ),
    )
    tab: str | None = Field(default=None, pattern=r"^t\d+$", description=_TAB_DESCRIPTION)
    new_tab: bool = Field(
        default=False,
        description="Open the URL in a new tab of this task; tab is ignored when true.",
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "BrowserNavigateArgs":
        if (self.url is None) == (self.action is None):
            raise ValueError("provide exactly one of url or action")
        if self.new_tab and self.url is None:
            raise ValueError("new_tab needs a url")
        return self


class BrowserSnapshotArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    tab: str | None = Field(default=None, pattern=r"^t\d+$", description=_TAB_DESCRIPTION)
    from_line: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Line to start from when a long snapshot was cut off; the cut-off note gives the "
            "number. Pass null to read from the top."
        ),
    )


# ---------------------------------------------------------------------------
# browser_act：一组动作，按 type 区分；元素一律用快照里的 ref 指定。ref 按节点稳定（宿主
# ``ref-registry.ts``），任何一次快照里的 ref 在元素还在页面上时都可用，导航后全部失效。
# 宿主 ``params.ts`` 是同一契约在进程边界上的校验点，这里是给模型看的 schema，两边同步修改。
# ---------------------------------------------------------------------------

_REF_PATTERN = r"^e\d+$"
_REF = Field(
    pattern=_REF_PATTERN,
    description="Element ref such as e12 from a snapshot of the current page.",
)


class ClickAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["click"]
    ref: str = _REF


class HoverAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["hover"]
    ref: str = Field(
        pattern=_REF_PATTERN,
        description="Element to move the mouse over, e.g. to open a menu that shows on hover.",
    )


class TypeAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["type"]
    ref: str = _REF
    text: str = Field(max_length=10_000, description="Text to type into the field.")
    clear: bool = Field(default=False, description="Clear the field first.")
    submit: bool = Field(default=False, description="Press Enter after typing.")


_NamedKey = Literal[
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
_CharacterKey = Annotated[str, Field(pattern=r"^[a-z0-9]$")]


class PressAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["press"]
    key: _NamedKey | _CharacterKey = Field(
        description="A named key, or a single lowercase letter or digit for shortcuts."
    )
    modifiers: list[Literal["Alt", "Control", "Meta", "Shift", "ControlOrMeta"]] | None = Field(
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
        pattern=_REF_PATTERN,
        description="Focus this element before pressing; null presses on the focused element.",
    )


class SelectAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["select"]
    ref: str = _REF
    values: list[str] = Field(
        min_length=1,
        max_length=20,
        description=(
            "Option values or visible labels to select. Native select lists only; for a custom "
            "dropdown, click the field and then the option."
        ),
    )


class ScrollAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["scroll"]
    direction: Literal["up", "down"] | None = Field(
        default=None,
        description=(
            "Scroll by about one screenful. Without ref this scrolls the page; with ref it "
            "scrolls the scrollable area containing that element (lists, chat logs, panels). "
            "Fails with SCROLL_END when it cannot move further."
        ),
    )
    ref: str | None = Field(
        default=None,
        pattern=_REF_PATTERN,
        description=(
            "With a direction: the element whose scrollable area to scroll. With a null "
            "direction: bring this element into view, e.g. before a screenshot."
        ),
    )

    # 宿主同样拒绝两者皆空；在这里拦下，模型拿到的是参数校验错误而不是链路错误。
    @model_validator(mode="after")
    def _direction_or_ref(self) -> "ScrollAction":
        if self.direction is None and self.ref is None:
            raise ValueError("scroll needs a direction, a ref, or both")
        return self


class WaitForAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["wait_for"]
    text: str | None = Field(
        default=None, min_length=1, description="Wait until this text appears on the page."
    )
    text_gone: str | None = Field(
        default=None,
        min_length=1,
        description="Wait until this text is gone from the page, e.g. a loading message.",
    )
    seconds: float | None = Field(
        default=None,
        gt=0,
        le=15,
        description=(
            "With text or text_gone: the maximum wait (null means 5). Otherwise: the length of a "
            "plain pause."
        ),
    )

    # 宿主做同样的判断（INVALID_PARAMS）；在这里拦下，模型拿到的是参数校验错误而不是链路错误。
    @model_validator(mode="after")
    def _one_condition(self) -> "WaitForAction":
        if self.text is not None and self.text_gone is not None:
            raise ValueError("wait_for takes text or text_gone, not both")
        if self.text is None and self.text_gone is None and self.seconds is None:
            raise ValueError("wait_for needs text, text_gone or seconds")
        return self


BrowserAction = (
    ClickAction
    | HoverAction
    | TypeAction
    | PressAction
    | SelectAction
    | ScrollAction
    | WaitForAction
)


class BrowserActArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    actions: list[BrowserAction] = Field(
        min_length=1,
        max_length=10,
        description="Actions to run in order; stops at the first failure.",
    )
    tab: str | None = Field(default=None, pattern=r"^t\d+$", description=_TAB_DESCRIPTION)


class BrowserScreenshotArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    full_page: bool = Field(
        default=False, description="Capture the whole page (height capped) instead of the viewport."
    )
    tab: str | None = Field(default=None, pattern=r"^t\d+$", description=_TAB_DESCRIPTION)


# list 不需要 tab；select / close 需要。
class BrowserTabsArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal["list", "select", "close"] = Field(
        description=(
            "list this task's tabs, select one as the current tab, or close one you no longer need."
        )
    )
    tab: str | None = Field(
        default=None, pattern=r"^t\d+$", description="Tab id for select or close; null for list."
    )

    @model_validator(mode="after")
    def _tab_for_select_and_close(self) -> "BrowserTabsArgs":
        if self.action != "list" and self.tab is None:
            raise ValueError(f"{self.action} needs a tab")
        return self


class BrowserHandoffArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    reason: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "What the user should do on the page, e.g. 'sign in'. It is shown to the user, so "
            "write it in the user's language."
        ),
    )
    tab: str | None = Field(default=None, pattern=r"^t\d+$", description=_TAB_DESCRIPTION)
