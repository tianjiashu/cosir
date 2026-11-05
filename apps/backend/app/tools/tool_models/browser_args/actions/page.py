"""页面状态类动作：选择、滚动、等待、回答弹窗、上传文件。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tools.tool_models.browser_args._shared import REF_FIELD, REF_PATTERN


class SelectAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["select"]
    ref: str = REF_FIELD
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
        pattern=REF_PATTERN,
        description=(
            "With a direction: the element whose scrollable area to scroll. With a null "
            "direction: bring this element into view, e.g. before a screenshot."
        ),
    )

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

    @model_validator(mode="after")
    def _one_condition(self) -> "WaitForAction":
        if self.text is not None and self.text_gone is not None:
            raise ValueError("wait_for takes text or text_gone, not both")
        if self.text is None and self.text_gone is None and self.seconds is None:
            raise ValueError("wait_for needs text, text_gone or seconds")
        return self


# 页面的 JS 弹窗（alert / confirm / prompt）在作答前挡住页面；只有导航与关闭标签时宿主才按取消
# 代答。编号防止答错弹窗：Agent 看到的弹窗可能已被用户答掉、换成另一个。带 id 只回答该弹窗；省略
# 时只能回答同一次调用里前面的动作弹出的（如先 click 删除再 dialog accept），否则宿主以
# DIALOG_MISMATCH 失败；没有弹窗时以 NO_DIALOG 失败。
class DialogAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["dialog"]
    accept: bool = Field(
        description="true to accept (OK), false to dismiss (Cancel) the open page dialog."
    )
    id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Dialog id from a browser result; only that dialog is answered. Leave null only "
            "for a dialog opened by an earlier action in this same call (e.g. click Delete, "
            "then dialog accept); otherwise the action fails with DIALOG_MISMATCH."
        ),
    )
    text: str | None = Field(
        default=None,
        max_length=10_000,
        description=(
            "Answer for a prompt dialog when accepting; null keeps the default answer. Ignored "
            "for other dialogs."
        ),
    )


# 把本机文件交给页面的文件输入框。网站常把 <input type=file> 藏起来、用按钮打开选择框，所以
# 两种指法：ref 指向文件输入框本身；或省略 ref，回答页面在 Agent 的动作之后打开的选择框（先
# click「上传」按钮）。路径与 read_file 同一套读规则，后端解析成绝对路径并确认是文件后才交给
# 宿主（不存在的路径 CDP 会静默放进一个空文件，实测）。
class UploadAction(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["upload"]
    paths: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Files to upload: paths relative to the workspace root, or absolute paths.",
    )
    ref: str | None = Field(
        default=None,
        pattern=REF_PATTERN,
        description=(
            "The file input (input type=file) to fill. Leave null to answer the file chooser "
            "the page opened after your action, e.g. click an Upload button first in the same "
            "call."
        ),
    )
