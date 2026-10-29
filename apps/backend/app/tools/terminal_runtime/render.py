"""把一次观察渲染为 Agent 可读的文本：一两行元信息 + ``行号│文本``，以及后台结论载荷。

格式与 ``read_file`` 一致（行号 + 文本），不用逐行 JSON。预算按字符数计，沿用
``Tools.MAX_OUTPUT_CHARS`` 并预留 handler 包装余量；超预算时：

- ``both``（write / 条件等待的新增行）：先按头 20 + 尾 60 取窗，再在预算内优先保留尾部，
  中间以 ``--- omitted lines a-b ---`` 标出，Agent 可按行号补读；
- ``tail``（默认读取尾部）：从前端裁掉旧行；
- ``head``（按 start_line 读取）：从后端截断，并提示下次的 start_line。

全屏程序（备用屏幕）期间默认给「屏幕视图」：整屏物理行以 ``s1│`` 起的屏幕行号返回，与逻辑行号
分属两套编号，模型不会把重绘中的屏幕行当作可按行号引用的历史；按 start_line 读取仍读冻结的主屏
逻辑行。全屏程序原地重绘、不追加行，因此 write 不按「输入后新增行」取窗，而是返回整屏。

纯函数，不持有状态、不做 IO：会话侧只读事实由 runtime 在会话锁内取成 ``SessionFacts`` 传入，
「交付后清空用户输入标注」这类写操作留在 runtime。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from app.config.constant import Constant
from app.tools.terminal_runtime.observation import TerminalObservation

Line = tuple[int, str]
KeepMode = Literal["both", "tail", "head"]

_SEPARATOR = "│"


@dataclass(frozen=True)
class RenderedLines:
    """渲染结果：正文行、实际返回的首末行号与省略区间。"""

    body: tuple[str, ...]
    first: int | None
    last: int | None
    omitted: tuple[int, int] | None
    truncated_after: int | None


def budget_chars() -> int:
    return Constant.Tools.MAX_OUTPUT_CHARS - 1024


def clip(text: str) -> str:
    limit = Constant.Terminal.OBSERVATION_LINE_MAX_CHARS
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit} chars)"


def _row(line: Line, prefix: str = "") -> str:
    return f"{prefix}{line[0]}{_SEPARATOR}{clip(line[1])}"


def render_lines(
    lines: Sequence[Line], mode: KeepMode, budget: int, *, prefix: str = ""
) -> RenderedLines:
    """在 ``budget`` 字符内渲染行；``lines`` 已按行号升序且连续。

    ``prefix`` 区分编号体系：逻辑行无前缀，屏幕视图为 ``s``（只用于 tail / head）。
    """
    if not lines:
        return RenderedLines((), None, None, None, None)
    if mode == "both":
        return _render_both(lines, budget)
    rows = [_row(line, prefix) for line in lines]
    used = 0
    if mode == "tail":
        start = len(rows)
        while start > 0 and used + len(rows[start - 1]) + 1 <= budget:
            start -= 1
            used += len(rows[start]) + 1
        start = min(start, len(rows) - 1)  # 至少保留最后一行（单行已由 clip 限长）
        return RenderedLines(tuple(rows[start:]), lines[start][0], lines[-1][0], None, None)
    end = 0
    while end < len(rows) and used + len(rows[end]) + 1 <= budget:
        used += len(rows[end]) + 1
        end += 1
    end = max(end, 1)
    truncated = lines[end][0] if end < len(lines) else None
    return RenderedLines(tuple(rows[:end]), lines[0][0], lines[end - 1][0], None, truncated)


def _render_both(lines: Sequence[Line], budget: int) -> RenderedLines:
    head_n = Constant.Terminal.OBSERVATION_HEAD_LINES
    tail_n = Constant.Terminal.OBSERVATION_TAIL_LINES
    rows = [_row(line) for line in lines]
    if len(rows) <= head_n + tail_n and sum(len(row) + 1 for row in rows) <= budget:
        return RenderedLines(tuple(rows), lines[0][0], lines[-1][0], None, None)
    # 头部候选是前 head_n 行，尾部候选是后 tail_n 行（行数不足 head_n + tail_n 时两者重叠，
    # 由下面「尾部先取、头部取到尾部起点为止」消解）。尾部候选不能为空：行数少但超预算时若把预算
    # 全给头部，会丢掉最该看的结尾（报错通常在那里）。
    head_end = min(head_n, len(rows))
    tail_start = max(0, len(rows) - tail_n)
    # 报错通常在尾部、上下文在头部：尾部优先占用预算（约七成），剩余给头部。
    used = 0
    tail_budget = budget * 7 // 10
    start = len(rows)
    while start > tail_start and used + len(rows[start - 1]) + 1 <= tail_budget:
        start -= 1
        used += len(rows[start]) + 1
    end = 0
    while end < min(head_end, start) and used + len(rows[end]) + 1 <= budget:
        used += len(rows[end]) + 1
        end += 1
    if end == 0 and start == len(rows):
        start = len(rows) - 1
    omitted = (lines[end][0], lines[start - 1][0]) if end < start else None
    body = rows[:end]
    if omitted is not None:
        body.append(f"--- omitted lines {omitted[0]}-{omitted[1]} (read with start_line) ---")
    body.extend(rows[start:])
    return RenderedLines(tuple(body), lines[0][0], lines[-1][0], omitted, None)


@dataclass(frozen=True)
class LineSearch:
    """read 的搜索请求：``first..last`` 内只保留命中 ``pattern`` 的行，最多 ``max_matches`` 条。"""

    pattern: re.Pattern[str]
    max_matches: int


@dataclass(frozen=True)
class TerminalObservationResult:
    """一次 Agent 观察（write / read）的结果：模型文本 + 展示所需的结构化事实。"""

    session_id: str
    status: str
    text: str
    # 逻辑行区间；屏幕视图时为 None（屏幕行号不是可引用的行号），由 ``screen`` 标出。
    lines: tuple[int, int] | None = None
    screen: bool = False
    wait_result: dict[str, object] | None = None
    input_type: str | None = None
    input_summary: str | None = None


@dataclass(frozen=True)
class SessionFacts:
    """渲染所需的会话侧事实快照（runtime 持锁时取）；``ended`` 为已离开 starting / running。"""

    session_id: str
    status: str
    ended: bool
    end_reason: str | None
    exit_code: int | None
    user_typing: bool
    handed_off: bool
    user_marks: tuple[int, int] | None
    # 用户在全屏程序里有输入：屏幕行没有稳定行号，只能标出「有输入」。
    user_typed_in_screen: bool = False


def render_observation(
    facts: SessionFacts,
    observation: TerminalObservation,
    first: int,
    last: int,
    mode: KeepMode,
    *,
    logical: bool = False,
    search: LineSearch | None = None,
    wait_result: dict[str, object] | None = None,
    note: str | None = None,
    input_type: str | None = None,
    input_summary: str | None = None,
) -> TerminalObservationResult:
    """渲染观察文本：表头 + 区间说明 + 预算内的行。

    全屏程序期间默认给屏幕视图（整屏 ``s<n>│文本``，``first..last`` 不适用：程序原地重绘、不追加
    行）；``logical=True``（read 指定 start_line 或搜索）时仍按逻辑行读冻结的主屏 ``first..last``。
    ``search`` 只保留命中行（行号不连续），超出命中上限或预算时提示续搜的 start_line。
    """
    buffer = observation.buffer
    header = _header_lines(facts, observation, wait_result, note, input_type, input_summary)
    budget = budget_chars() - sum(len(line) + 1 for line in header) - 200
    screen = buffer.alternate and not logical
    if screen:
        rendered = render_lines(buffer.screen_lines(), "tail", budget, prefix="s")
        range_line = _screen_range_line(facts, observation, rendered)
    elif search is not None:
        first = max(first, buffer.first_available_line)
        matches = [line for line in buffer.get(first, last) if search.pattern.search(line[1])]
        rendered = render_lines(matches[: search.max_matches], "head", budget)
        if rendered.truncated_after is None and len(matches) > search.max_matches:
            rendered = replace(rendered, truncated_after=matches[search.max_matches][0])
        range_line = _search_range_line(
            facts, observation, search, first, last, len(matches), rendered
        )
    else:
        rendered = render_lines(
            buffer.get(max(first, buffer.first_available_line), last), mode, budget
        )
        range_line = _range_line(facts, observation, rendered)
    # 屏幕视图的行号不是可引用的逻辑行号，不给区间。
    lines = (
        (rendered.first, rendered.last)
        if not screen and rendered.first is not None and rendered.last is not None
        else None
    )
    return TerminalObservationResult(
        session_id=facts.session_id,
        status=facts.status,
        text="\n".join([*header, range_line, *rendered.body]),
        lines=lines,
        screen=screen,
        wait_result=wait_result,
        input_type=input_type,
        input_summary=input_summary,
    )


def session_state_parts(facts: SessionFacts, observation: TerminalObservation) -> list[str]:
    """会话此刻状态的短语（shell、生命周期、在做什么、集成、cwd、全屏、输入权）。

    read / write 的 header 与 terminal_list 共用这一份措辞，Agent 在两处看到的状态写法一致。
    """
    buffer = observation.buffer
    parts = [observation.shell_kind, facts.status]
    command = observation.active_command
    if facts.ended:
        if facts.end_reason and facts.end_reason != facts.status:
            parts.append(str(facts.end_reason))
        if facts.exit_code is not None:
            parts.append(f"exit {facts.exit_code}")
    elif command is not None:
        # 用户启动的命令要让 Agent 一眼看到：它在跑时 Agent 输入会被拒，先换会话而不是去撞。
        owner = ", started by user" if command.started_by == "user" else ""
        parts.append(f"executing `{command.command}` (cmd {command.number}{owner})")
    elif observation.phase == "editing":
        parts.append("at prompt")
    if observation.integration_status != "ready":
        parts.append(f"shell integration {observation.integration_status}")
    if observation.cwd:
        parts.append(f"cwd {observation.cwd}")
    if buffer.alternate:
        parts.append(f"full-screen app (alternate screen {buffer.rows}x{buffer.columns})")
    if facts.user_typing:
        parts.append("user is typing")
    if facts.handed_off:
        parts.append("handed off to user")
    return parts


def latest_command_phrase(observation: TerminalObservation) -> str | None:
    """最近一条已结束命令的结果与输出行区间；仍在执行（已体现在状态里）或没有命令时为 None。"""
    latest = observation.latest_command
    if latest is None or latest.status == "executing":
        return None
    text = f"cmd {latest.number} `{latest.command}` {latest.status}"
    if latest.status == "returned" and latest.result is not None:
        text += f" exit {latest.result.get('code')}"
    output = latest.output_lines
    return text + (f" · output lines {output[0]}-{output[1]}" if output else " · no output")


def render_session_summary(
    facts: SessionFacts,
    observation: TerminalObservation,
    *,
    label: str | None,
    created_by: str,
) -> str:
    """terminal_list 的单会话一行：身份、创建者、当前状态与最近命令结果。"""
    name = f"session {facts.session_id}" + (f' "{label}"' if label else "")
    parts = [f"{name} by {created_by}", *session_state_parts(facts, observation)]
    latest = latest_command_phrase(observation)
    if latest is not None:
        parts.append(f"last {latest}")
    return " · ".join(parts)


def render_end_notice(
    facts: SessionFacts,
    observation: TerminalObservation,
    *,
    label: str | None,
    created_by: str,
) -> str:
    """Agent 创建的会话意外结束时给它的系统通知正文：会话摘要（含终态与最近命令）+ 尾部若干行。"""
    buffer = observation.buffer
    summary = render_session_summary(facts, observation, label=label, created_by=created_by)
    tail = Constant.Terminal.END_NOTICE_TAIL_LINES
    rendered = render_lines(
        buffer.get(buffer.last_line - tail + 1, buffer.last_line), "tail", budget_chars() // 4
    )
    text = (
        f"A terminal session you started has ended: {summary}. Anything that relied on it "
        "(a dev server, a watcher) is gone; start a new session if you still need it."
    )
    if rendered.body:
        text += "\nLast lines:\n" + "\n".join(rendered.body)
    return text


def _header_lines(
    facts: SessionFacts,
    observation: TerminalObservation,
    wait_result: dict[str, object] | None,
    note: str | None,
    input_type: str | None,
    input_summary: str | None,
) -> list[str]:
    header = [" · ".join([f"session {facts.session_id}", *session_state_parts(facts, observation)])]
    result: list[str] = []
    if input_type is not None:
        result.append(f"input {input_type} `{input_summary}`")
    if wait_result is not None:
        reason = wait_result["end_reason"]
        text = f"wait={wait_result['until']} → {reason}"
        if "matched_line" in wait_result:
            text += f" at line {wait_result['matched_line']}"
        if "matched_screen_row" in wait_result:
            text += f" at s{wait_result['matched_screen_row']}"
        result.append(text)
    latest = latest_command_phrase(observation)
    if latest is not None:
        result.append(latest)
    if note:
        result.append(note)
    if result:
        header.append("result: " + " · ".join(result))
    return header


def _screen_range_line(
    facts: SessionFacts, observation: TerminalObservation, rendered: RenderedLines
) -> str:
    buffer = observation.buffer
    rows = (
        "no screen rows"
        if rendered.first is None
        else f"screen rows s{rendered.first}-s{rendered.last}"
    )
    text = (
        f"{rows} · cursor s{buffer.screen_cursor_row} · main screen lines "
        f"{buffer.first_available_line}-{buffer.last_line} are kept as they were before the app "
        "started (read them with start_line)"
    )
    if facts.user_typed_in_screen:
        text += " · user typed in this app since your last look"
    return text


def _search_range_line(
    facts: SessionFacts,
    observation: TerminalObservation,
    search: LineSearch,
    first: int,
    last: int,
    total: int,
    rendered: RenderedLines,
) -> str:
    buffer = observation.buffer
    text = (
        f"{total} lines match /{search.pattern.pattern}/ in lines {first}-{last} "
        f"(of {buffer.first_available_line}-{buffer.last_line}, live from {buffer.live_from_line})"
    )
    if rendered.first is not None:
        text += f" · showing {len(rendered.body)} from {rendered.first} to {rendered.last}"
    if rendered.truncated_after is not None:
        text += f" · more matches, continue with start_line={rendered.truncated_after}"
    if facts.user_marks is not None:
        text += f" · user typed lines {facts.user_marks[0]}-{facts.user_marks[1]}"
    return text


def _range_line(
    facts: SessionFacts, observation: TerminalObservation, rendered: RenderedLines
) -> str:
    buffer = observation.buffer
    text = "no lines" if rendered.first is None else f"lines {rendered.first}-{rendered.last}"
    text += (
        f" of {buffer.first_available_line}-{buffer.last_line}"
        f" (live from {buffer.live_from_line}) · cursor line {buffer.cursor_line}"
    )
    if rendered.truncated_after is not None:
        text += f" · truncated, continue with start_line={rendered.truncated_after}"
    if facts.user_marks is not None:
        text += f" · user typed lines {facts.user_marks[0]}-{facts.user_marks[1]}"
    return text
