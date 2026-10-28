"""把一次观察渲染为 Agent 可读的文本：一两行元信息 + ``行号│文本``，以及后台结论载荷。

格式与 ``read_file`` 一致（行号 + 文本），不用逐行 JSON。预算按字符数计，沿用
``Tools.MAX_OUTPUT_CHARS`` 并预留 handler 包装余量；超预算时：

- ``both``（write / 条件等待的新增行）：先按头 20 + 尾 60 取窗，再在预算内优先保留尾部，
  中间以 ``--- omitted lines a-b ---`` 标出，Agent 可按行号补读；
- ``tail``（默认读取尾部）：从前端裁掉旧行；
- ``head``（按 start_line 读取）：从后端截断，并提示下次的 start_line。

纯函数，不持有状态、不做 IO：会话侧只读事实由 runtime 在会话锁内取成 ``SessionFacts`` 传入，
「交付后清空用户输入标注」这类写操作留在 runtime。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.config.constant import Constant
from app.tools.terminal_runtime.terminal_observation import TerminalObservation

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


def _row(line: Line) -> str:
    return f"{line[0]}{_SEPARATOR}{clip(line[1])}"


def render_lines(lines: Sequence[Line], mode: KeepMode, budget: int) -> RenderedLines:
    """在 ``budget`` 字符内渲染行；``lines`` 已按行号升序且连续。"""
    if not lines:
        return RenderedLines((), None, None, None, None)
    if mode == "both":
        return _render_both(lines, budget)
    rows = [_row(line) for line in lines]
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
class TerminalObservationResult:
    """一次 Agent 观察（write / read）的结果：模型文本 + 展示所需的结构化事实。"""

    session_id: str
    status: str
    text: str
    lines: tuple[int, int] | None = None
    wait_result: dict[str, object] | None = None
    input_type: str | None = None
    input_summary: str | None = None
    suspended: bool = False
    # 本观察是延迟结果的后台占位（后台等待、交接、转到后台），见 ``ToolObservation.deferred``。
    deferred: bool = False


@dataclass(frozen=True)
class SessionFacts:
    """渲染所需的会话侧事实快照（runtime 持锁时取）；``ended`` 为已离开 starting / running。"""

    session_id: str
    shell_kind: str
    status: str
    ended: bool
    end_reason: str | None
    exit_code: int | None
    user_typing: bool
    handed_off: bool
    user_marks: tuple[int, int] | None


def render_observation(
    facts: SessionFacts,
    observation: TerminalObservation,
    first: int,
    last: int,
    mode: KeepMode,
    *,
    wait_result: dict[str, object] | None = None,
    note: str | None = None,
    input_type: str | None = None,
    input_summary: str | None = None,
) -> TerminalObservationResult:
    """渲染 ``first..last`` 的观察文本：表头 + 行区间说明 + 预算内的行。"""
    buffer = observation.buffer
    first = max(first, buffer.first_available_line)
    lines = buffer.get(first, last)
    header = _header_lines(facts, observation, wait_result, note, input_type, input_summary)
    budget = budget_chars() - sum(len(line) + 1 for line in header) - 200
    rendered = render_lines(lines, mode, budget)
    header.append(_range_line(facts, observation, rendered))
    text = "\n".join([*header, *rendered.body])
    return TerminalObservationResult(
        session_id=facts.session_id,
        status=facts.status,
        text=text,
        lines=(
            (rendered.first, rendered.last)
            if rendered.first is not None and rendered.last is not None
            else None
        ),
        wait_result=wait_result,
        input_type=input_type,
        input_summary=input_summary,
    )


def background_payload(
    facts: SessionFacts, observation: TerminalObservation, wait_result: dict[str, object]
) -> dict[str, Any]:
    """后台结论给模型的 JSON——会话、结束原因、命令结果与尾部若干行。"""
    buffer = observation.buffer
    tail = Constant.Terminal.BACKGROUND_RESULT_TAIL_LINES
    rendered = render_lines(
        buffer.get(buffer.last_line - tail + 1, buffer.last_line), "tail", budget_chars() // 2
    )
    return {
        "session_id": facts.session_id,
        "status": facts.status,
        "wait": wait_result,
        "command": observation.command_summary(),
        "lines": f"{buffer.first_available_line}-{buffer.last_line}",
        "tail": "\n".join(rendered.body),
    }


def _header_lines(
    facts: SessionFacts,
    observation: TerminalObservation,
    wait_result: dict[str, object] | None,
    note: str | None,
    input_type: str | None,
    input_summary: str | None,
) -> list[str]:
    buffer = observation.buffer
    parts = [f"session {facts.session_id}", facts.shell_kind, facts.status]
    command = observation.active_command
    if facts.ended:
        parts.append(f"{facts.end_reason} exit {facts.exit_code}")
    elif command is not None:
        parts.append(f"executing `{command['command']}` (cmd {command['number']})")
    elif observation.phase == "editing":
        parts.append("at prompt")
    if observation.integration_status != "ready":
        parts.append(f"shell integration {observation.integration_status}")
    if observation.cwd:
        parts.append(f"cwd {observation.cwd}")
    if buffer.alternate:
        parts.append(f"alternate screen {buffer.rows}x{buffer.columns}")
    if facts.user_typing:
        parts.append("user is typing")
    if facts.handed_off:
        parts.append("handed off to user")
    header = [" · ".join(parts)]
    result: list[str] = []
    if input_type is not None:
        result.append(f"input {input_type} `{input_summary}`")
    if wait_result is not None:
        reason = wait_result["end_reason"]
        text = f"wait={wait_result['until']} → {reason}"
        if "matched_line" in wait_result:
            text += f" at line {wait_result['matched_line']}"
        result.append(text)
    latest = observation.command_summary()
    if latest is not None and latest["status"] != "executing":
        text = f"cmd {latest['number']} `{latest['command']}` {latest['status']}"
        if latest["status"] == "returned":
            text += f" exit {latest['result'].get('code')}"
        output = latest.get("output_lines")
        text += f" · output lines {output[0]}-{output[1]}" if output else " · no output"
        result.append(text)
    if note:
        result.append(note)
    if result:
        header.append("result: " + " · ".join(result))
    return header


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
