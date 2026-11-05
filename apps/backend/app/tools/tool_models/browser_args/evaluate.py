"""browser_evaluate：在页面里求值一段表达式。"""

from pydantic import BaseModel, ConfigDict, Field

from app.tools.tool_models.browser_args._shared import tab_field


class BrowserEvaluateArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    expression: str = Field(
        min_length=1,
        max_length=20_000,
        description=(
            "JavaScript expression evaluated in the page; its value comes back as JSON. Use an "
            "IIFE such as (() => { ... })() for statements; a returned Promise is awaited."
        ),
    )
    tab: str | None = tab_field()
