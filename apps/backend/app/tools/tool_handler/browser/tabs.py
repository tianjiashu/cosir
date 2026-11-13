"""``browser_tabs``：列出、切换、关闭本 Session 的标签页。"""

from typing import ClassVar

from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_BROWSER_TABS,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_BROWSER
from app.tools.host.errors import HostCallError
from app.tools.tool_handler.browser.errors import browser_error_observation
from app.tools.tool_handler.browser.host_request import call_browser, tool_timeout
from app.tools.tool_handler.browser.rendering import tabs_text
from app.tools.tool_models import BrowserTabsArgs


class BrowserTabsTool(HandlerBase):
    """新开标签不在这里：``browser_navigate`` 的 ``new_tab``，开的同时就有页面可看。"""

    name = TOOL_BROWSER_TABS
    # 中文译文：管理本会话的内置浏览器标签页：list 列出（标出当前标签与用户开的标签），select
    # 把某个标签设为当前（省略 tab 的调用都用它），close 关闭不再需要的标签。新开标签用
    # browser_navigate 的 new_tab。本会话的标签数有上限（只有几个），到上限后复用已有标签或先关掉
    # 不用的，不要一直开新的。
    description = (
        "Manage this session's built-in browser tabs: list them (the current one and the ones the "
        "user opened are marked), select one as current (calls that omit tab use it), or close "
        "tabs you no longer need. To open a new tab use browser_navigate with new_tab. A session "
        "holds only a few tabs, so reuse or close them instead of opening more."
    )
    args_model = BrowserTabsArgs
    timeout_seconds: ClassVar[float] = tool_timeout("browser.tabs")
    group = TOOL_GROUP_BROWSER

    def execute(
        self, action: str, tab: str | None, *, execution_context: ToolExecutionContext
    ) -> ToolObservation:
        try:
            tabs = call_browser(
                "browser.tabs",
                execution_context,
                action=action,
                tab=tab,
            )
        except HostCallError as error:
            return browser_error_observation(self.name, error)
        return tool_success(self.name, tabs_text(tabs))

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("browser",),
            execution_mode="thread",
            parallel_mode="serial",
        )
