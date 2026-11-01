"""内置浏览器 Agent 工具与注册定义构建入口。

浏览器本身在桌面宿主（Electron main）里，工具经宿主工具链路（``app.tools.host.call_host``）调用
``browser.*`` 方法；标签页是宿主事实，后端不保存。
"""

from app.core.tool.schemas import ToolDefinition
from app.tools.tool_handler.browser.act import BrowserActTool
from app.tools.tool_handler.browser.handoff import BrowserHandoffTool
from app.tools.tool_handler.browser.navigate import BrowserNavigateTool
from app.tools.tool_handler.browser.screenshot import BrowserScreenshotTool
from app.tools.tool_handler.browser.snapshot import BrowserSnapshotTool
from app.tools.tool_handler.browser.tabs import BrowserTabsTool


def build_browser_definitions() -> list[ToolDefinition]:
    return [
        tool.to_definition()
        for tool in (
            BrowserNavigateTool(),
            BrowserSnapshotTool(),
            BrowserActTool(),
            BrowserScreenshotTool(),
            BrowserTabsTool(),
            BrowserHandoffTool(),
        )
    ]


__all__ = [
    "BrowserActTool",
    "BrowserHandoffTool",
    "BrowserNavigateTool",
    "BrowserScreenshotTool",
    "BrowserSnapshotTool",
    "BrowserTabsTool",
    "build_browser_definitions",
]
