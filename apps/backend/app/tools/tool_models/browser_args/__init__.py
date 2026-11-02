"""内置浏览器工具的输入契约。

参数模型不写类 docstring：它会作为顶层 description 进入模型可见 schema，说明改写成注释。
标签归属取自执行上下文，模型不能指定别的 Task；``tab`` 只在本 Task 的标签之间选择。

这些模型也是宿主的参数契约：``scripts/export_browser_tool_schema.py`` 把 ``model_json_schema()``
导出到 ``apps/desktop/electron/main/browser/contract/tool-args.schema.json``，宿主按它校验进程
边界上的参数（守卫测试核对导出文件与模型一致）。JSON Schema 表达不了的跨字段规则
（``model_validator``）只在这里，宿主信任后端已校验过。

一个工具一个模块；动作族的十个变体在 :mod:`.actions`，跨工具共用的字段约束在 :mod:`._shared`。
"""

from app.tools.tool_models.browser_args.act import BrowserActArgs
from app.tools.tool_models.browser_args.evaluate import BrowserEvaluateArgs
from app.tools.tool_models.browser_args.handoff import BrowserHandoffArgs
from app.tools.tool_models.browser_args.logs import BrowserLogsArgs
from app.tools.tool_models.browser_args.navigate import BrowserNavigateArgs
from app.tools.tool_models.browser_args.screenshot import BrowserScreenshotArgs
from app.tools.tool_models.browser_args.snapshot import BrowserSnapshotArgs
from app.tools.tool_models.browser_args.tabs import BrowserTabsArgs

__all__ = [
    "BrowserActArgs",
    "BrowserEvaluateArgs",
    "BrowserHandoffArgs",
    "BrowserLogsArgs",
    "BrowserNavigateArgs",
    "BrowserScreenshotArgs",
    "BrowserSnapshotArgs",
    "BrowserTabsArgs",
]
