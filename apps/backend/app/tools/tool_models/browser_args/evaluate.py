"""browser_evaluate：在页面里求值一段表达式。"""

from pydantic import BaseModel, ConfigDict, Field

from app.tools.tool_models.browser_args._shared import REF_PATTERN, tab_field


class BrowserEvaluateArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    expression: str = Field(
        min_length=1,
        max_length=20_000,
        description=(
            "JavaScript expression evaluated in the page; its value comes back as JSON. Use an "
            "IIFE such as (() => { ... })() for statements; a returned Promise is awaited. With "
            "ref, the element is available as el, e.g. el.value."
        ),
    )
    # 在元素所在框架的主世界里求值：跨源、跨进程 iframe 里的元素也够得着（宿主 ``page-evaluate.ts``）。
    ref: str | None = Field(
        default=None,
        pattern=REF_PATTERN,
        description=(
            "Element ref such as e12 from a snapshot: the expression runs with that element as el, "
            "inside the frame it belongs to, which also reaches elements in cross-origin iframes. "
            "Null to run in the page's main frame."
        ),
    )
    tab: str | None = tab_field()
