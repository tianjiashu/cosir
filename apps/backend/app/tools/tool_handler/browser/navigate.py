"""``browser_navigate``：在内置浏览器里打开地址或做历史导航，返回导航后的页面快照。"""

from typing import Any, ClassVar

from app.core.tool.schemas import (
    TOOL_BROWSER_NAVIGATE,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_BROWSER
from app.tools.host.errors import HostCallError
from app.tools.tool_handler.browser.errors import browser_error_observation
from app.tools.tool_handler.browser.host_request import call_browser, tool_timeout
from app.tools.tool_handler.browser.rendering import page_observation
from app.tools.tool_models import BrowserNavigateArgs


class BrowserNavigateTool(HandlerBase):
    """Task 还没有标签时由宿主新开一个；返回快照，模型不必再调 ``browser_snapshot``。"""

    name = TOOL_BROWSER_NAVIGATE
    # 中文译文：在内置浏览器里打开网页（http/https）或本机文件（绝对路径或 file:// 地址，如生成的
    # HTML），或在标签里后退、前进、刷新。返回加载后页面的快照，可交互元素带 ref，之后通常不必再调
    # browser_snapshot。本任务还没有标签时会自动新开一个；new_tab 在另一个标签里打开。
    description = (
        "Open a web page (http/https) or a local file (absolute path or file:// URL, such as "
        "generated HTML) in the built-in browser, or go back, forward or reload a tab. Returns a "
        "snapshot of the loaded page, with refs for interactive elements, so you rarely need "
        "browser_snapshot afterwards. A tab is opened automatically if this task has none yet; "
        "new_tab opens the URL in another tab."
    )
    args_model = BrowserNavigateArgs
    timeout_seconds: ClassVar[float] = tool_timeout("browser.navigate")
    group = TOOL_GROUP_BROWSER

    def execute(
        self,
        url: str | None,
        action: str | None,
        tab: str | None,
        new_tab: bool,
        viewport: dict[str, Any] | None,
        color_scheme: str | None,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        try:
            page = call_browser(
                "browser.navigate",
                execution_context,
                url=url,
                action=action,
                tab=tab,
                new_tab=new_tab,
                viewport=viewport,
                color_scheme=color_scheme,
            )
        except HostCallError as error:
            return browser_error_observation(self.name, error)
        return page_observation(self.name, page)

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
