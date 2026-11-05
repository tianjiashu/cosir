"""browser_handoff：把页面交给用户操作（HITL 交接）。"""

from pydantic import BaseModel, ConfigDict, Field

from app.tools.tool_models.browser_args._shared import tab_field


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
    tab: str | None = tab_field()
