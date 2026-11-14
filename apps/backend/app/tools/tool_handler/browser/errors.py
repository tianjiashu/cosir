"""浏览器能力层错误码的解释：给模型的下一步建议与卡片短提示。

错误码的唯一登记处是宿主 ``apps/desktop/electron/main/browser/errors.ts`` 的
``BrowserErrorCode``；登记表里除链路层同名的 ``INVALID_PARAMS`` / ``CANCELLED`` /
``UNAVAILABLE``（交给 ``host_error_observation``）以外，每个码都必须在下面的表里，由
``tests/test_browser_contract_guard.py`` 双向核对。
"""

from __future__ import annotations

from app.core.tool.results.tool_error import tool_error
from app.core.tool.schemas import ToolObservation
from app.tools.host.errors import HostCallError
from app.tools.host.host_call import host_error_observation

# 错误码 → (给模型的下一步建议, 卡片短提示)。一张表：同一个码既可能让整次调用失败，
# 也可能让 browser_act 停在某一步（失败的动作之前的动作已经生效，结果里随后附上页面的变化），
# 建议写成两种场合都成立的说法。短提示在整次失败时显示在卡片上，browser_act 停在某一步时
# 显示在卡片的那一步上（``failure_hint``）。
BROWSER_ERRORS: dict[str, tuple[str, str]] = {
    "TAB_BUSY": (
        "The previous operation is still settling; observe the page before trying another action.",
        "操作尚未结束",
    ),
    "OPERATION_UNKNOWN": (
        "Input state is unknown. Do not repeat the action. Close this tab "
        "and open a new one if needed.",
        "输入状态未知",
    ),
    "HANDED_OFF": (
        "This tab belongs to the user until they hand it back. Read-only observation "
        "is still available.",
        "等待用户交还",
    ),
    "TAB_NOT_FOUND": (
        "Pass one of this session's tab ids (see browser_tabs), or open a page with browser_navigate.",
        "标签不存在",
    ),
    "TAB_LIMIT": (
        "Reuse one of this session's open tabs by passing its tab id, or close one with "
        "browser_tabs; if the limit is the global one and this session has none, wait for other "
        "sessions to close tabs.",
        "标签已满",
    ),
    "NAVIGATION_BLOCKED": (
        "Only web pages (http, https) and local files can be opened.",
        "地址被拦截",
    ),
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
    "DIALOG_OPEN": (
        "Answer it first with a browser_act dialog action on the tab showing it, using the dialog "
        "id given above; if that tab is not one of this session's tabs, wait for the user to answer "
        "it.",
        "页面有弹窗",
    ),
    "DIALOG_MISMATCH": (
        "Answer the dialog that is open now by its id; omit the id only for a dialog opened by "
        "an earlier action in the same call.",
        "弹窗不对应",
    ),
    "NO_DIALOG": ("No dialog is open; check the latest snapshot.", "没有弹窗"),
    "REF_STALE": ("Use refs from the latest snapshot.", "元素已失效"),
    "NO_FILE_INPUT": (
        "Give the ref of a file input (input type=file), or click the button that opens the "
        "file chooser and then upload without a ref in the same call.",
        "不是文件框",
    ),
    "OUT_OF_VIEWPORT": (
        "Coordinates are pixels of a viewport screenshot of the page as it is now; scroll to "
        "bring the spot into view, or use a ref.",
        "超出视口",
    ),
    "NOT_INTERACTABLE": (
        "Scroll it into view or pick another element from the latest snapshot.",
        "元素不可见",
    ),
    "WAIT_TIMEOUT": (
        "The page may still be loading or have changed; check the latest snapshot.",
        "等待超时",
    ),
    "DEADLINE_EXCEEDED": (
        "The call ran out of time (a slow or stuck page, or too many slow steps in one call); "
        "anything already done took effect. Check the page, then continue in smaller steps.",
        "操作超时",
    ),
    "SCROLL_END": (
        "Nothing further that way. If the page loads more as you scroll, wait briefly and "
        "scroll again.",
        "已到底",
    ),
    "ACTION_FAILED": ("Check the latest snapshot before trying again.", "执行失败"),
}


def failure_advice(code: str) -> str:
    """``browser_act`` 停下时的建议；未登记的码（如链路层的 ``CANCELLED``）按 ``ACTION_FAILED``。"""

    return BROWSER_ERRORS.get(code, BROWSER_ERRORS["ACTION_FAILED"])[0]


def failure_hint(code: str) -> str:
    """``browser_act`` 停下的那一步在卡片上的短提示；链路层的取消单独说明，未登记的码按执行失败。"""

    if code == "CANCELLED":
        return "已取消"
    return BROWSER_ERRORS.get(code, BROWSER_ERRORS["ACTION_FAILED"])[1]


def browser_error_observation(tool_name: str, error: HostCallError) -> ToolObservation:
    known = BROWSER_ERRORS.get(error.code)
    if known is None:
        return host_error_observation(tool_name, error)
    advice, hint = known
    return tool_error(tool_name, f"{error.message}. {advice}", status_hint=hint)
