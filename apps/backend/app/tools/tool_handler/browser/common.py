"""浏览器工具共用：组装宿主方法参数、把页面结果写成模型正文、解释浏览器自己的错误码。

宿主方法与结果字段见 ``apps/desktop/electron/main/browser/``
（``methods.ts`` / ``browser-host.ts``）。
"""

from __future__ import annotations

from typing import Any

from app.config.settings import Settings
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolExecutionContext, ToolObservation
from app.core.workflow.message_queue.message_bodies.rendering import escape_tag
from app.tools.display.browser_display import build_browser_page_display_data
from app.tools.host.errors import HostCallError
from app.tools.host.host_call import host_error_observation

SNAPSHOT_TAG = "page_snapshot"

# 浏览器能力层错误码 → (给模型的下一步建议, 卡片短提示)。链路层错误码交给 host_error_observation。
_BROWSER_ERRORS: dict[str, tuple[str, str]] = {
    "TAB_NOT_FOUND": (
        "Omit tab to use this task's current tab, or open a page with browser_navigate.",
        "标签不存在",
    ),
    "TAB_LIMIT": ("Reuse one of this task's open tabs by passing its tab id.", "标签已满"),
    "NAVIGATION_BLOCKED": ("Only http and https pages can be opened.", "地址被拦截"),
    "NAVIGATION_FAILED": (
        "Check the address; retry only if the failure looks temporary.",
        "页面加载失败",
    ),
    "PAGE_CRASHED": ("Reload the tab or navigate elsewhere to recover.", "页面崩溃"),
    "PAGE_UNRESPONSIVE": (
        "The page is busy or stuck; wait briefly, then reload it.",
        "页面无响应",
    ),
    "USER_ACTIVE": (
        "Wait a few seconds and try again, or use browser_handoff if the user should finish it.",
        "用户正在操作",
    ),
}

# browser_act 中途停下时，各错误码的下一步建议（失败的动作之前的动作已经生效）。
_ACTION_ADVICE: dict[str, str] = {
    "REF_STALE": "Use the refs in the snapshot below.",
    "NOT_INTERACTABLE": "Scroll it into view or pick another element from the snapshot below.",
    "WAIT_TIMEOUT": "The page may still be loading or have changed; check the snapshot.",
    "SCROLL_END": (
        "Nothing further that way. If the page loads more as you scroll, wait briefly and "
        "scroll again."
    ),
    "ACTION_FAILED": "Check the snapshot below before trying again.",
    "CANCELLED": "The call was cancelled.",
}


def host_params(execution_context: ToolExecutionContext, **params: Any) -> dict[str, Any]:
    """注入归属 Task 与当前标签上限（事实源是配置中心，每次现读，保存后下一次调用即生效）。"""

    return {
        "task_id": execution_context.task_id,
        "limits": {
            "per_task": Settings.BROWSER_MAX_TABS_PER_TASK,
            "total": Settings.BROWSER_MAX_TABS,
        },
        **{key: value for key, value in params.items() if value is not None},
    }


def page_observation(
    tool_name: str,
    page: dict[str, Any],
    *,
    lead: str | None = None,
    display_data: dict[str, Any] | None = None,
) -> ToolObservation:
    """把宿主返回的页面写成模型正文：（可选的）结论句、页头、备注，以及放在边界标签里的快照。"""

    header = f"tab {page['tab_id']} · {page['title'] or '(untitled)'} · {page['url']}"
    if page["loading"]:
        header += " (still loading)"
    lines = [lead, header] if lead else [header]
    if page["other_tabs"]:
        lines.append(f"other tabs in this task: {', '.join(page['other_tabs'])}")
    if page["frames"]:
        lines.append(
            f"the page has {page['frames']} embedded frame(s) whose contents are not shown"
        )
    if page["notes"]:
        lines.append("since the last look at this tab:")
        lines.extend(f"- {note}" for note in page["notes"])
    snapshot = escape_tag(page["snapshot"], SNAPSHOT_TAG)
    lines.append(f"<{SNAPSHOT_TAG}>\n{snapshot}\n</{SNAPSHOT_TAG}>")
    return tool_success(
        tool_name=tool_name,
        content="\n".join(lines),
        display_data=display_data or build_browser_page_display_data(page),
    )


def act_summary(outcome: dict[str, Any], total: int) -> str:
    """``browser_act`` 的结论句：全部完成，或停在第几步、为什么、下一步怎么办。"""

    failure = outcome["failure"]
    if failure is None:
        return f"completed all {total} action(s)."
    advice = _ACTION_ADVICE.get(failure["code"], _ACTION_ADVICE["ACTION_FAILED"])
    return (
        f"completed {outcome['completed']} of {total} action(s); action {failure['index'] + 1} "
        f"failed ({failure['code']}): {failure['message']}. {advice}"
    )


def browser_error_observation(tool_name: str, error: HostCallError) -> ToolObservation:
    known = _BROWSER_ERRORS.get(error.code)
    if known is None:
        return host_error_observation(tool_name, error)
    advice, hint = known
    return tool_error(tool_name, f"{error.message}. {advice}", status_hint=hint)
