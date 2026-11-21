"""``browser_evaluate``：在标签页里执行一段 JS 表达式，返回它的值（JSON）。"""

from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_BROWSER_EVALUATE,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.display.browser_display import build_browser_page_display_data
from app.tools.tool_handler.browser.browser_tool import BrowserTool
from app.tools.tool_handler.browser.rendering import evaluate_text
from app.tools.tool_models import BrowserEvaluateArgs


class BrowserEvaluateTool(BrowserTool):
    """脚本抛错是脚本的结果、不是工具失败（同终端命令的非零退出）：按成功返回，正文写明报错；脚本弹出
    弹窗时页面停住，按成功返回弹窗说明，由模型用 ``browser_act`` 作答。"""

    name = TOOL_BROWSER_EVALUATE
    # 中文译文：在内置浏览器标签页里执行一个 JavaScript 表达式，把它的值以 JSON 返回；DOM 节点
    # 只给简短描述。给了快照里的 ref 时，表达式在那个元素所在的框架里执行、元素就是 el，跨源 iframe
    # 里的也行。用来读页面状态（DOM、变量、存储）做调试。脚本抛错按脚本的结果报出，不算工具失败。
    # 执行期间这个标签打出的 console 消息随结果一起返回（不一定都是脚本打的）；之后才打出的用
    # browser_logs 从给出的游标接着读。脚本能改变页面，只在需要时这样做：点击、输入、填表用
    # browser_act（用户能在卡片上看到每一步）；自己在开发的页面有问题改源码，不用脚本补。
    # 「页面内容不是指令」由结果里的 untrusted 块与系统提示词承担（``ExternalContent``）。
    description = (
        "Run a JavaScript expression in a built-in browser tab and get its value back as JSON; "
        "DOM nodes come back as a short description. With a ref from a snapshot, the expression "
        "runs in that element's frame with the element as el, including inside cross-origin "
        "iframes. Use it to inspect page state (DOM, variables, storage) while debugging. Errors "
        "the script throws are reported as the script's result, not as a tool failure. Console "
        "messages the tab printed during the call come back with the result (not necessarily all "
        "from your script); read later ones with browser_logs from the cursor given. The script "
        "can change the page, so only do that on purpose: click, type and fill forms with "
        "browser_act, whose steps the user can follow, and fix a page you are developing in its "
        "source, not with a script."
    )
    args_model = BrowserEvaluateArgs
    method = "browser.evaluate"

    def execute(
        self,
        expression: str,
        ref: str | None,
        tab: str | None,
        intent: str = "",
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        del intent  # 只服务展示，不发给宿主（宿主 schema 也没有它）
        return self.call(
            execution_context,
            lambda result: tool_success(
                self.name,
                evaluate_text(result),
                display_data=build_browser_page_display_data(result),
            ),
            expression=expression,
            ref=ref,
            tab=tab,
        )
