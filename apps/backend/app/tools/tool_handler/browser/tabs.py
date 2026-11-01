"""``browser_tabs``：列出、切换、关闭本 Task 的标签页。"""

from typing import ClassVar

from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_BROWSER_TABS,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_BROWSER
from app.tools.host.errors import HostCallError
from app.tools.host.host_call import call_host
from app.tools.tool_handler.browser.common import browser_error_observation, host_params
from app.tools.tool_models import BrowserTabsArgs

_HOST_TIMEOUT_SECONDS = 10.0


class BrowserTabsTool(HandlerBase):
    """新开标签不在这里：``browser_navigate`` 的 ``new_tab``，开的同时就有页面可看。"""

    name = TOOL_BROWSER_TABS
    # 中文译文：管理本任务的内置浏览器标签页：list 列出（标出当前标签与用户开的标签），select
    # 把某个标签设为当前（省略 tab 的调用都用它），close 关闭不再需要的标签。新开标签用
    # browser_navigate 的 new_tab。本任务的标签数有上限（只有几个），到上限后复用已有标签或先关掉
    # 不用的，不要一直开新的。
    description = (
        "Manage this task's built-in browser tabs: list them (the current one and the ones the "
        "user opened are marked), select one as current (calls that omit tab use it), or close "
        "tabs you no longer need. To open a new tab use browser_navigate with new_tab. A task "
        "holds only a few tabs, so reuse or close them instead of opening more."
    )
    args_model = BrowserTabsArgs
    timeout_seconds: ClassVar[float] = _HOST_TIMEOUT_SECONDS + 5
    group = TOOL_GROUP_BROWSER

    def execute(
        self, action: str, tab: str | None, *, execution_context: ToolExecutionContext
    ) -> ToolObservation:
        try:
            tabs = call_host(
                "browser.tabs",
                host_params(execution_context, action=action, tab=tab),
                execution_context=execution_context,
                timeout_seconds=_HOST_TIMEOUT_SECONDS,
            )
        except HostCallError as error:
            return browser_error_observation(self.name, error)
        # 标出用户开的标签（与 terminal_list 标出会话创建者一致）：用户可能正在用它。
        lines = [
            f"{item['tab_id']}{' (current)' if item['current'] else ''}"
            f"{' (opened by the user)' if item['created_by'] == 'user' else ''} · "
            f"{item['title'] or '(untitled)'} · {item['url']}"
            for item in tabs
        ]
        return tool_success(self.name, "\n".join(lines) or "this task has no open tabs")

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
            display=ToolDisplayHints(verb="管理标签页", icon="globe"),
        )
