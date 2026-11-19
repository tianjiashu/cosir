"""把宿主返回的浏览器结果写成模型正文（纯函数）。

结果形状的跨语言样例是 ``apps/desktop/electron/main/browser/contract/wire-samples.json``，
``tests/test_browser_contract_guard.py`` 用这里的函数逐个渲染样例：字段改名或缺失会在那里以
``KeyError`` 暴露。

格式：页头与结论写成 ``键: 值`` 的短行（值里可能出现 ``·``，按位置分隔会读错），大段内容放进边界标签；
怎么用游标、怎么读日志这类不随结果变的说明归工具描述，不在每次结果里重复。

信任边界：网站给出的字符串一律经 ``ExternalContent`` 放进 untrusted 块（来源是标签地址的主机名）：页面
标题、快照、备注（含网站给的下载文件名、新窗口地址）、弹窗消息、日志条目、响应体、脚本的值与报错。块外只写
宿主确定的事实（标签 id、地址、状态码、视口）与工具自己的说明，结果末尾一句收尾。每个结果一个
``_PageRender``，块与收尾句同属这一个结果。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tool.results.external_content import ExternalContent
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolObservation
from app.tools.display.browser_display import build_act_steps, build_browser_page_display_data
from app.tools.tool_handler.browser.errors import failure_advice, failure_hint

TITLE_TAG = "page_title"
NOTES_TAG = "page_notes"
TABS_TAG = "tab_list"
SNAPSHOT_TAG = "page_snapshot"
DIALOG_TAG = "dialog_message"
SCRIPT_RESULT_TAG = "script_result"
SCRIPT_ERROR_TAG = "script_error"
LOG_TAGS = {"console": "console_messages", "network": "network_requests"}
BODY_TAG = "response_body"
_LOG_NOUNS = {"console": "console message", "network": "network request"}


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


class _PageRender:
    """一个标签页结果的渲染：网站给的文字以这个标签的主机名为来源进 untrusted 块，``close`` 定稿并追加收尾句。"""

    def __init__(self, head: dict[str, Any]) -> None:
        self._external = ExternalContent()
        self._source = ExternalContent.source_of(head["url"])
        self._head = head

    def block(self, tag: str, text: str) -> str:
        return self._external.block(tag, text, self._source)

    def close(self, lines: list[str]) -> str:
        return self._external.close("\n".join(lines))

    def header(self, *, viewport: bool = True) -> list[str]:
        """页头：标签、地址（HTTP 状态码、加载中）与标题（网站给的，进块），页面类结果另带生效的视口与模拟。

        ``status`` 为 null（如 about:blank）时不写。页面、act 与截图结果写视口：模型据它判断看到的是桌面还是
        手机布局、坐标按多大算，默认值也不必在后端另存一份；日志与脚本结果用不上，``viewport=False`` 省掉。
        """

        head = self._head
        states = [f"HTTP {head['status']}"] if head["status"] is not None else []
        if head["loading"]:
            states.append("still loading")
        url = f"url: {head['url']}"
        if states:
            url += f" ({', '.join(states)})"
        title = self.block(TITLE_TAG, head["title"]) if head["title"] else "title: (untitled)"
        lines = [f"tab: {head['tab_id']}", url, title]
        if viewport:
            emulation = head["emulation"]
            line = f"viewport: {emulation['width']}×{emulation['height']}"
            if emulation["mobile"]:
                line += " mobile"
            if emulation["color_scheme"] is not None:
                line += f", {emulation['color_scheme']}"
            if emulation["follows_panel"]:
                # 跟随面板的视口会随用户调整面板而变：坐标以最近一次截图为准。
                line += " (follows the browser panel)"
            lines.append(line)
        return lines

    def dialog_notice(self, dialog: dict[str, Any]) -> str:
        """页面被 JS 弹窗停住时的说明：弹窗文字（含 prompt 默认值）进 untrusted 块。"""

        kind = dialog["type"]
        message = [f"message: {dialog['message']}"]
        if kind == "prompt":
            default = json.dumps(dialog["default_text"], ensure_ascii=False)
            message.append(f"default answer (used when accepting without text): {default}")
        return "\n".join(
            [
                f"the page is showing dialog {dialog['id']} ({kind}) and is paused until it is "
                "answered:",
                self.block(DIALOG_TAG, "\n".join(message)),
                f"Answer it with a browser_act dialog action with id {dialog['id']} (accept or "
                "dismiss) before doing anything else on this tab.",
            ]
        )


def _new_logs_line(new_logs: dict[str, int]) -> str | None:
    counts = [
        (new_logs["console_errors"], "console error"),
        (new_logs["console_warnings"], "console warning"),
        (new_logs["failed_requests"], "failed request"),
    ]
    parts = [_counted(count, noun) for count, noun in counts if count]
    if not parts:
        return None
    # 怎么读（不带 since、按 level / failed_only 筛）写在 browser_logs 的描述里。
    return f"new logs since this tab's last page result: {', '.join(parts)} (see browser_logs)"


def page_text(page: dict[str, Any], *, lead: str | None = None) -> str:
    """页面结果（navigate / snapshot / act）：结论句、页头、上下文、快照或弹窗说明。

    页面被本标签的 JS 弹窗挡住时（``dialog`` 非空）宿主读不到快照，正文改为说明弹窗与作答方式。
    """

    render = _PageRender(page)
    lines = [lead] if lead else []
    lines.extend(render.header())
    if page["other_tabs"]:
        lines.append(f"other tabs: {', '.join(page['other_tabs'])}")
    if page["frames"]:
        # 没展开的子框架没有任何路能读进快照：直说，并指出截图能看到它们显示的内容。
        lines.append(
            f"frames not shown: {page['frames']} (their contents are not in this snapshot; "
            "browser_screenshot shows what they display)"
        )
    new_logs = _new_logs_line(page["new_logs"])
    if new_logs is not None:
        lines.append(new_logs)
    if page["notes"]:
        # 备注由宿主写成句子，但引用了网站给的文字（下载文件名、新窗口地址），整体进块。
        lines.append("notes since this tab's last page result:")
        lines.append(render.block(NOTES_TAG, "\n".join(f"- {note}" for note in page["notes"])))
    if page["dialog"] is not None:
        lines.append(render.dialog_notice(page["dialog"]))
    else:
        lines.append(render.block(SNAPSHOT_TAG, page["snapshot"]))
    return render.close(lines)


def act_summary(outcome: dict[str, Any]) -> str:
    """``browser_act`` 的结论：做完几步；没全做完时逐步列出结局，再写停在哪、下一步怎么办。

    停下的原因常常就是失败那一步的错误（样例里两者相同），相同时只写建议、不再复述错误。「做完不代表网站
    接受了」是每次都成立的说明，写在工具描述里。步骤的目标说明（元素文字）来自网页，只给卡片、不进正文：
    模型按序号就知道是哪一步。
    """

    execution = outcome["execution"]
    steps = execution["steps"]
    completed = sum(step["status"] == "done" for step in steps)
    # 宿主按动作逐一回报（跳过的也在），步数就是动作数；`build_act_steps` 的 strict zip 守着这一点。
    lines = [f"actions: {completed} of {len(steps)} done"]
    if completed < len(steps):
        for index, step in enumerate(steps):
            line = f"- {index + 1} {step['status']}"
            if step["error"] is not None:
                line += f" ({step['error']['code']}: {step['error']['message']})"
            lines.append(line)
    interruption = execution["interruption"]
    if interruption is not None:
        advice = failure_advice(interruption["code"])
        if any(step["error"] == interruption for step in steps):
            lines.append(f"stopped: {advice}")
        else:
            lines.append(f"stopped: {interruption['code']}: {interruption['message']}. {advice}")
    if any(step["status"] == "unknown" for step in steps):
        lines.append(
            "unknown: that input may already have taken effect; observe the page before "
            "continuing and do not replay it."
        )
    return "\n".join(lines)


def act_observation(
    tool_name: str, outcome: dict[str, Any], actions: list[dict[str, Any]]
) -> ToolObservation:
    """执行回执独立于观察；页面已关闭或读不到时仍把已知事实交给模型和 UI。"""
    lead = act_summary(outcome)
    page = outcome["observation"]
    if page is not None:
        content = page_text(page, lead=lead)
    else:
        error = outcome["observation_error"]
        render = _PageRender(outcome)
        lines = [lead, *render.header()]
        if error["code"] == "TAB_NOT_FOUND":
            lines.append("the tab was closed; the header above is the page before the actions.")
        lines.append(
            f"page observation unavailable: {error['code']}: {error['message']}. "
            f"{failure_advice(error['code'])} Read a fresh snapshot before continuing."
        )
        content = render.close(lines)
    return tool_success(
        tool_name=tool_name,
        content=content,
        display_data=build_browser_page_display_data(
            page or outcome, steps=build_act_steps(actions, outcome, failure_hint)
        ),
    )


def _console_entry(entry: dict[str, Any]) -> str:
    line = f"#{entry['seq']} [{entry['level']}] {entry['text']}"
    if entry["source"] is not None:
        line += f" (at {entry['source']})"
    if entry["frame"] is not None:
        line += f" (in frame {entry['frame']})"
    # 多行文本（堆栈）缩进续行，条目边界才分得清。
    return line.replace("\n", "\n    ")


def _size(size: int) -> str:
    return f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB"


def _body_label(body: dict[str, Any]) -> str:
    """列表行上的响应体状态（方案「响应体状态表」）：模型据此决定读哪一条，不必挨个试。"""

    state, mime, size = body["state"], body["mime"], body["size"]
    if state == "text":
        return f"body: {mime or 'text'}, {_size(size)}"
    if state == "binary":
        return f"body: {mime or 'binary'}, {_size(size)} transferred, not text"
    if state == "too_large":
        return f"body: too large to keep ({_size(size)} transferred)"
    return {
        "pending": "body: capturing",
        "deferred": "body: waits for the dialog to be answered",
        "empty": "body: empty",
        "evicted": "body: dropped for newer ones",
        "streaming": "body: stream, none",
        "unavailable": "body: not captured",
    }[state]


def _network_line(entry: dict[str, Any]) -> str:
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


def _network_entry(entry: dict[str, Any]) -> str:
    line = _network_line(entry)
    return line if entry["body"] is None else f"{line} · {_body_label(entry['body'])}"


def response_body_text(result: dict[str, Any]) -> str:
    """``browser_logs`` 读正文：页头、请求本身，再是正文（边界标签内）或没有正文的原因。"""

    render = _PageRender(result)
    lines = render.header(viewport=False)
    lines.append(f"request: {_network_line(result['entry'])}")
    body = result["body"]
    if body["text"] is None:
        lines.append(f"no body: {body['reason']}")
        return render.close(lines)
    described = f"{body['mime'] or 'text'}, {_size(body['size'])}"
    if body["truncated"]:
        described += f"; only the first {_size(len(body['text'].encode('utf-8')))} were kept"
    lines.append(f"response body ({described}):")
    lines.append(render.block(BODY_TAG, body["text"]))
    return render.close(lines)


def logs_text(result: dict[str, Any]) -> str:
    """``browser_logs``：页头、条目（边界标签内）与游标；读正文的结果交给 ``response_body_text``。"""

    kind = result["kind"]
    if kind == "response_body":
        return response_body_text(result)
    noun = _LOG_NOUNS[kind]
    render = _PageRender(result)
    lines = render.header(viewport=False)
    entries = result["entries"]
    if entries:
        entry_line = _console_entry if kind == "console" else _network_entry
        lines.append(f"{_counted(len(entries), noun)}, oldest first:")
        lines.append(
            render.block(LOG_TAGS[kind], "\n".join(entry_line(entry) for entry in entries))
        )
    else:
        lines.append(f"no matching {noun}s.")
    if result["dropped_before"] is not None:
        lines.append(
            f"dropped: entries before #{result['dropped_before']} (over capacity, cannot be read)"
        )
    if result["more_before"]:
        lines.append(
            "older: more matching entries of this page not shown (raise limit, or pass an earlier since)"
        )
    follows = " (more matching entries follow)" if result["more_after"] else ""
    lines.append(f"next: since={result['next']}{follows}")
    # 网络条目在请求开始时取序号、结局之后才补；用游标读过的在途请求不会因 since 重读出现，
    # 明确告诉模型最终结局从哪里看（省略 since 的默认读取）。
    if kind == "network" and any(
        entry["status"] is None and entry["failure"] is None for entry in entries
    ):
        lines.append("requests with no response yet: read without since later for their outcome.")
    return render.close(lines)


def evaluate_text(result: dict[str, Any]) -> str:
    """``browser_evaluate``：页头，加上返回值、脚本报错或脚本弹出的弹窗三者之一，再附执行期间的 console。

    console 条目与 ``browser_logs`` 同一渲染与边界标签；游标总是给出，求值返回之后才异步打出的日志从它接着读。
    """

    render = _PageRender(result)
    lines = render.header(viewport=False)
    if result["dialog"] is not None:
        lines.append(
            "the script opened a page dialog and is paused on it; this call has ended without a "
            "value. Once the dialog is answered the script carries on to its end, but its value "
            "is not returned: do not run it again to finish it (that repeats its effects); if "
            "you need a value, read it with a separate expression that changes nothing."
        )
        lines.append(render.dialog_notice(result["dialog"]))
    elif result["error"] is not None:
        lines.append("script error (no value returned):")
        lines.append(render.block(SCRIPT_ERROR_TAG, result["error"]))
    elif result["value"] == "undefined":
        lines.append("value: undefined")
    else:
        note = ", cut off at the size limit; return a smaller part" if result["truncated"] else ""
        lines.append(f"value (JSON{note}):")
        lines.append(render.block(SCRIPT_RESULT_TAG, result["value"]))
    console = result["console"]
    if console["entries"]:
        lines.append(
            f"{_counted(len(console['entries']), 'console message')} during this call, oldest first:"
        )
        lines.append(
            render.block(
                LOG_TAGS["console"],
                "\n".join(_console_entry(entry) for entry in console["entries"]),
            )
        )
    follows = "more messages follow; " if console["more_after"] else ""
    lines.append(
        f"later console output: {follows}browser_logs kind=console since={console['next']}"
    )
    return render.close(lines)


_TAB_OPERATIONS = {
    "running": "an operation is still running",
    "stopping": "an operation is stopping",
    "unknown": "last input state unknown",
}


def tabs_text(tabs: list[dict[str, Any]], action: str = "list", tab: str | None = None) -> str:
    """``browser_tabs``：select / close 先写一行结论，再列出操作之后的标签。

    宿主在操作完成后才取列表，所以结论行只复述这次做了什么，不必让模型对比前后两次列表。标出用户开的标签
    （与 terminal_list 标出会话创建者一致：用户可能正在用它）与未收敛的修改操作（这时再操作会得到
    ``TAB_BUSY`` / ``OPERATION_UNKNOWN``，先看列表就能避开）。

    列表里有各个网站的标题，整张列表进一个 untrusted 块，来源写「open tabs」：标题来自不同网站，按行拆块
    只会成倍增加标签。
    """

    lines = [] if action == "list" else [f"{'selected' if action == 'select' else 'closed'}: {tab}"]
    if not tabs:
        lines.append("this session has no open tabs")
        return "\n".join(lines)
    listed = []
    for item in tabs:
        marks = ["current"] if item["current"] else []
        if item["created_by"] == "user":
            marks.append("opened by the user")
        if item["operation"] is not None:
            marks.append(_TAB_OPERATIONS[item["operation"]])
        label = f"{item['tab_id']} ({', '.join(marks)})" if marks else item["tab_id"]
        listed.append(f"{label} · {item['title'] or '(untitled)'} · {item['url']}")
    external = ExternalContent()
    lines.append(external.block(TABS_TAG, "\n".join(listed), "open tabs"))
    return external.close("\n".join(lines))


def screenshot_text(
    shot: dict[str, Any], *, ref: str | None = None, region: bool = False, full_page: bool = False
) -> str:
    """``browser_screenshot``：页头（视口截图的像素尺寸就是视口尺寸），说明截的是什么，图片随后附上。

    取景优先级与宿主一致（``page-capture.ts``：元素 > 区域 > 整页）。元素与区域截图带边距、小的会放大，
    整页截图从页面顶端算起：三者的像素都不是视口坐标，坐标别从图上量。整页截图只在宿主报告真的截断了
    （``full_page_cut``）时说明页面多高、图里有多少、怎么看剩下的；没截断时不提，免得模型以为下面还有。
    """

    if ref:
        what = f"element {ref}; pixels are not viewport coordinates"
    elif region:
        what = "the given region; pixels are not viewport coordinates"
    elif full_page:
        what = "the whole page from its top; pixels are not viewport coordinates"
        cut = shot["full_page_cut"]
        if cut is not None:
            what += (
                f"; the page is {cut['page_height']}px tall and this image covers only the top "
                f"{cut['shown_height']}px: scroll down and take viewport screenshots to see the rest"
            )
    else:
        what = "the viewport"
    render = _PageRender(shot)
    return render.close([*render.header(), f"screenshot: {what} (the image follows)"])


def page_observation(tool_name: str, page: dict[str, Any]) -> ToolObservation:
    return tool_success(
        tool_name=tool_name,
        content=page_text(page),
        display_data=build_browser_page_display_data(page),
    )
