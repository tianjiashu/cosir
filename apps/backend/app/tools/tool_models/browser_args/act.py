"""browser_act：一组动作，按 ``type`` 区分；动作变体见 :mod:`.actions`。"""

from pydantic import BaseModel, ConfigDict, Field

from app.tools.tool_models.browser_args._shared import tab_field
from app.tools.tool_models.browser_args.actions import BrowserAction


class BrowserActArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    actions: list[BrowserAction] = Field(
        min_length=1,
        max_length=10,
        description="Actions to run in order; stops at the first failure.",
    )
    tab: str | None = tab_field()
