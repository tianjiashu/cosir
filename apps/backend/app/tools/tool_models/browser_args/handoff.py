"""browser_handoff：把页面交给用户操作（HITL 交接）。"""

from pydantic import BaseModel, ConfigDict, Field

from app.tools.tool_models.browser_args._shared import tab_field


class BrowserHandoffArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    reason: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "What the user should do on the page, e.g. 'sign in to github.com'. Name the site by "
            "the host in the tab's URL, not by what the page calls itself. It is shown to the "
            "user, so write it in the user's language."
        ),
    )
    tab: str | None = tab_field()
