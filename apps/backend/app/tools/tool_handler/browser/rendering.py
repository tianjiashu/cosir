"""把宿主返回的浏览器结果写成模型正文（纯函数）。

结果形状的跨语言样例是 ``apps/desktop/electron/main/browser/contract/wire-samples.json``，
``tests/test_browser_contract_guard.py`` 用这里的函数逐个渲染样例：字段改名或缺失会在那里以
``KeyError`` 暴露。

信任边界：快照、弹窗消息、日志条目、脚本的值与报错 ``escape_tag`` 后放进边界标签。页头与标签列表
里的页面标题、备注（含网站给的下载文件名、新窗口地址）目前仍在标签外，见 AGENTS.md 待办
「工具备注统一呈现」。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolObservation
from app.core.workflow.message_queue.message_bodies.rendering import escape_tag
from app.tools.display.browser_display import build_browser_page_display_data
from app.tools.tool_handler.browser.errors import failure_advice

SNAPSHOT_TAG = "page_snapshot"
DIALOG_TAG = "dialog_message"
SCRIPT_RESULT_TAG = "script_result"
SCRIPT_ERROR_TAG = "script_error"
LOG_TAGS = {"console": "console_messages", "network": "network_requests"}
_LOG_NOUNS = {"console": "console message", "network": "network request"}


def fenced(text: str, tag: str) -> str:
    return f"<{tag}>\n{escape_tag(text, tag)}\n</{tag}>"


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def header_lines(head: dict[str, Any]) -> list[str]:
    """页头：标签 · 标题 · 地址（加载中、HTTP 状态码），另起一行写生效的视口与模拟。

    所有页面类结果（页面、日志、脚本、截图）共用；``status`` 为 null（如 about:blank）时不写。
    视口总是写：模型据它判断看到的是桌面还是手机布局，默认值也不必在后端另存一份。
    """

    line = f"tab {head['tab_id']} · {head['title'] or '(untitled)'} · {head['url']}"
    if head["loading"]:
        line += " (still loading)"
    if head["status"] is not None:
        line += f" · HTTP {head['status']}"
    lines = [line]
    emulation = head["emulation"]
    viewport = f"viewport {emulation['width']}×{emulation['height']}"
    if emulation["mobile"]:
        viewport += " mobile"
    if emulation["follows_panel"]:
        # 跟随面板的视口会随用户调整面板而变：坐标以最近一次截图为准。
        viewport += " (follows the browser panel)"
    scheme = emulation["color_scheme"]
    lines.append(f"{viewport}, {scheme}" if scheme is not None else viewport)
    return lines


def _new_logs_line(new_logs: dict[str, int]) -> str | None:
    counts = [
        (new_logs["console_errors"], "console error"),
        (new_logs["console_warnings"], "console warning"),
        (new_logs["failed_requests"], "failed request"),
    ]
    parts = [_counted(count, noun) for count, noun in counts if count]
    if not parts:
        return None
    return (
        f"new on this page since this tab's last page result: {', '.join(parts)} "
        "(browser_logs shows them; narrow down with level or failed_only)"
    )


def dialog_notice(dialog: dict[str, Any]) -> str:
    """页面被 JS 弹窗停住时的说明：弹窗文字（含 prompt 默认值）进同一个边界标签。"""

    kind = dialog["type"]
    fenced_lines = [f"message: {dialog['message']}"]
    if kind == "prompt":
        default = json.dumps(dialog["default_text"], ensure_ascii=False)
        fenced_lines.append(f"default answer (used when accepting without text): {default}")
    return "\n".join(
        [
            f"the page is showing dialog {dialog['id']} ({kind}) and is paused until it is "
            "answered:",
            fenced("\n".join(fenced_lines), DIALOG_TAG),
            f"Answer it with a browser_act dialog action with id {dialog['id']} (accept or "
            "dismiss) before doing anything else on this tab.",
        ]
    )


def page_text(page: dict[str, Any], *, lead: str | None = None) -> str:
    """页面结果（navigate / snapshot / act / 交还）：结论句、页头、上下文、快照或弹窗说明。

    页面被本标签的 JS 弹窗挡住时（``dialog`` 非空）宿主读不到快照，正文改为说明弹窗与作答方式。
    """

    lines = [lead] if lead else []
    lines.extend(header_lines(page))
    if page["other_tabs"]:
        lines.append(f"other tabs in this task: {', '.join(page['other_tabs'])}")
    if page["frames"]:
        lines.append(
            f"the page has {page['frames']} embedded frame(s) whose contents are not shown"
        )
    new_logs = _new_logs_line(page["new_logs"])
    if new_logs is not None:
        lines.append(new_logs)
    if page["notes"]:
        lines.append("since this tab's last page result:")
        lines.extend(f"- {note}" for note in page["notes"])
    if page["dialog"] is not None:
        lines.append(dialog_notice(page["dialog"]))
    else:
        lines.append(fenced(page["snapshot"], SNAPSHOT_TAG))
    return "\n".join(lines)


def act_summary(outcome: dict[str, Any], total: int) -> str:
    """``browser_act`` 的结论句：全部完成，或停在第几步、为什么、下一步怎么办。"""

    failure = outcome["failure"]
    if failure is None:
        return f"completed all {total} action(s)."
    return (
        f"completed {outcome['completed']} of {total} action(s); action {failure['index'] + 1} "
        f"failed ({failure['code']}): {failure['message']}. "
        f"{failure_advice(failure['code'])}"
    )


def _console_entry(entry: dict[str, Any]) -> str:
    line = f"#{entry['seq']} [{entry['level']}] {entry['text']}"
    if entry["source"] is not None:
        line += f" (at {entry['source']})"
    if entry["frame"] is not None:
        line += f" (in frame {entry['frame']})"
    # 多行文本（堆栈）缩进续行，条目边界才分得清。
    return line.replace("\n", "\n    ")


def _network_entry(entry: dict[str, Any]) -> str:
    if entry["failure"] is not None:
        outcome = f"failed: {entry['failure']}"
    elif entry["status"] is not None:
        outcome = str(entry["status"])
    else:
        outcome = "no response yet"
    details = [entry["resource_type"]]
    if entry["duration_ms"] is not None:
        details.append(f"{entry['duration_ms']} ms")
    return f"#{entry['seq']} {entry['method']} {entry['url']} → {outcome} ({', '.join(details)})"


def logs_text(result: dict[str, Any]) -> str:
    """``browser_logs``：页头、条目（边界标签内）与游标说明。"""

    kind = result["kind"]
    noun = _LOG_NOUNS[kind]
    lines = header_lines(result)
    entries = result["entries"]
    if entries:
        render = _console_entry if kind == "console" else _network_entry
        lines.append(f"{_counted(len(entries), noun)}, oldest first:")
        lines.append(fenced("\n".join(render(entry) for entry in entries), LOG_TAGS[kind]))
    else:
        lines.append(f"no matching {noun}s.")
    if result["dropped_before"] is not None:
        lines.append(
            f"entries before #{result['dropped_before']} were dropped to stay within capacity "
            "and cannot be read."
        )
    if result["more_before"]:
        lines.append(
            "older matching entries of this page were not returned; raise limit, or pass since "
            "to read forward from an earlier number."
        )
    if result["more_after"]:
        lines.append(f"more matching entries follow; pass since={result['next']} to read on.")
    else:
        lines.append(f"to read only newer entries later, pass since={result['next']}.")
    return "\n".join(lines)


def evaluate_text(result: dict[str, Any]) -> str:
    """``browser_evaluate``：页头，加上返回值、脚本报错或脚本弹出的弹窗三者之一。"""

    lines = header_lines(result)
    if result["dialog"] is not None:
        lines.append(
            "the script opened a page dialog and is paused on it; this call has ended without a "
            "value. Once the dialog is answered the script carries on to its end, but its value "
            "is not returned: do not run it again to finish it (that repeats its effects); if "
            "you need a value, read it with a separate expression that changes nothing."
        )
        lines.append(dialog_notice(result["dialog"]))
    elif result["error"] is not None:
        lines.append("the script did not return a value:")
        lines.append(fenced(result["error"], SCRIPT_ERROR_TAG))
    elif result["value"] == "undefined":
        lines.append("the expression returned undefined.")
    else:
        note = " (cut off at the size limit; return a smaller part)" if result["truncated"] else ""
        lines.append(f"the expression's value as JSON{note}:")
        lines.append(fenced(result["value"], SCRIPT_RESULT_TAG))
    return "\n".join(lines)


def tabs_text(tabs: list[dict[str, Any]]) -> str:
    """``browser_tabs`` 的列表。标出用户开的标签（与 terminal_list 标出会话创建者一致）：
    用户可能正在用它。"""

    lines = [
        f"{item['tab_id']}{' (current)' if item['current'] else ''}"
        f"{' (opened by the user)' if item['created_by'] == 'user' else ''} · "
        f"{item['title'] or '(untitled)'} · {item['url']}"
        for item in tabs
    ]
    return "\n".join(lines) or "this task has no open tabs"


def screenshot_text(shot: dict[str, Any], ref: str | None = None) -> str:
    """``browser_screenshot``：页头（视口截图的像素尺寸就是视口尺寸），图片随后附上。元素截图的像素与
    视口坐标对不上（带边距、小元素放大），所以说明截的是哪个元素。"""

    what = f"screenshot of element {ref}" if ref else "screenshot of this tab"
    return "\n".join([*header_lines(shot), f"{what} (the image follows)."])


def page_observation(
    tool_name: str,
    page: dict[str, Any],
    *,
    lead: str | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> ToolObservation:
    return tool_success(
        tool_name=tool_name,
        content=page_text(page, lead=lead),
        display_data=build_browser_page_display_data(page, steps=steps),
    )
