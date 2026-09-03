"""CodeGraph 原始文本输出 → 客户端结构化列表的解析器。

单一职责：只把 vendor CodeGraph 工具返回的 markdown 风格文本解析成
``{"items": [...]}`` 结构，供前端 ``ToolListEntry`` 投影消费。不负责 RPC 调用、
不负责参数组装、不负责渲染（渲染一律由客户端完成）。

设计约束（见 docs/codegraph-structured-display-plan.md）：
- 只用标准库 ``re`` / ``dataclasses``，不引入新依赖。
- 解析失败**不抛异常、不静默丢弃**：降级为 ``{"raw": text}`` + 一条 warn 日志，
  保证 agent 仍拿得到原文，且问题可定位。
- ``codegraph_explore`` 本期不做结构化，恒走全文降级路径。

解析基准：``docs/codegraph-tool-outputs.md``（本项目真实 Kernel 输出样例）。
"""

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from app.config.logging.logger import log

# 降级日志中携带的原始文本片段长度上限，避免整页正文进日志。
_RAW_LOG_PREVIEW_CHARS = 200

# vendor 在正文前可能插入的「索引降级 / 陈旧」提示行前缀。
# 这些行属于运行状态提示而非查询结果，剥离后单独放入 notice，避免污染结构化正文。
_NOTICE_LINE_PATTERN = re.compile(r"^\s*(?:[⚠️!]|\*\*?(?:Note|Warning|Notice)\b)", re.IGNORECASE)

# `- name (kind) - path:line` 或 `- name (kind) - path:line — via edge`
_CALL_EDGE_ENTRY_PATTERN = re.compile(
    r"^-\s+(?P<name>.+?)\s+\((?P<kind>[^)]+)\)\s+-\s+"
    r"(?P<file_path>[^\s:]+):(?P<line>\d+)"
    r"(?:\s+[—-]+\s+via\s+(?P<edge>.+?))?\s*$"
)

# `**name** (kind)` —— search 结果与 node 头部共用的符号标题行。
_SYMBOL_HEADING_PATTERN = re.compile(r"^\*\*(?P<name>.+?)\*\*\s+\((?P<kind>[^)]+)\)\s*$")

# `path:line` —— search 结果中紧随标题行的位置行。
_LOCATION_LINE_PATTERN = re.compile(r"^(?P<file_path>[^\s:]+):(?P<line>\d+)\s*$")

# `` `signature` `` —— 反引号包裹的签名行。
_SIGNATURE_LINE_PATTERN = re.compile(r"^`(?P<signature>.*)`\s*$")

# `**Location:** path:line`
_NODE_LOCATION_PATTERN = re.compile(
    r"^\*\*Location:\*\*\s+(?P<file_path>[^\s:]+):(?P<line>\d+)\s*$"
)

# `**Signature:** `sig``
_NODE_SIGNATURE_PATTERN = re.compile(r"^\*\*Signature:\*\*\s+`(?P<signature>.*)`\s*$")

# `**Calls →** a (path:line), b (path:line)` / `**Called by ←** ...`
_NODE_TRAIL_PATTERN = re.compile(r"^\*\*(?P<label>Calls|Called by)\b[^*]*\*\*\s*(?P<body>.+?)\s*$")

# `name (path:line)` —— node trail 行内的单个条目。
_TRAIL_ENTRY_PATTERN = re.compile(r"^(?P<name>.+?)\s+\((?P<file_path>[^\s:]+):(?P<line>\d+)\)$")

# `**path:**` —— impact 的文件分节标题。
_IMPACT_FILE_HEADING_PATTERN = re.compile(r"^\*\*(?P<file_path>[^*]+?):\*\*\s*$")

# `sym:line` —— impact 分节内以逗号分隔的单个符号。
_IMPACT_SYMBOL_PATTERN = re.compile(r"^(?P<name>.+):(?P<line>\d+)$")


@dataclass(frozen=True)
class CodegraphListEntry:
    """一条 CodeGraph 结构化列表项（对应前端 ``ToolListEntry``）。

    字段命名刻意与前端 ``ToolListEntry`` 对齐（``filePath`` / ``lineNumber``），
    使前端投影层零字段改名、直接透传。

    属性:
        name: 符号名或文件名。
        kind: 符号类型（function/class/method/import/variable/file 等）；未知为空串。
        filePath: 相对项目根的文件路径；未解析到为空串。
        lineNumber: 1-based 行号；未解析到为 ``None``。
        edge: 调用边标签（如 ``import`` / ``instantiation``）；无为空串。
        signature: 符号签名；无为空串。
    """

    name: str
    kind: str = ""
    # filePath / lineNumber 刻意用 camelCase：与前端 ToolListEntry 字段名逐字对齐，
    # 使 asdict() 结果可直接透传，避免在投影层再写一层字段改名映射。
    filePath: str = ""
    lineNumber: int | None = None
    edge: str = ""
    signature: str = ""


@dataclass
class _ParsedText:
    """文本预处理结果：剥离 vendor 提示 banner 后的正文与提示行。

    属性:
        body: 剥离提示行后的正文（供各 parser 解析）。
        notice: 被剥离的提示行合并文本；无提示时为空串。
    """

    body: str
    notice: str = ""
    lines: list[str] = field(default_factory=list)


def parse_tool_result(tool: str, text: str) -> dict[str, object]:
    """把 CodeGraph 工具原始文本解析为客户端可消费的结构化字典。

    参数:
        tool: 工具名（``codegraph_search`` / ``codegraph_node`` /
            ``codegraph_callers`` / ``codegraph_callees`` / ``codegraph_impact`` /
            ``codegraph_explore``）。未知工具与 explore 一并走全文降级。
        text: vendor 返回并聚合后的原始文本。

    返回:
        结构化字典，恒含 ``tool`` 键；解析成功时含 ``items``（``list[dict]``，
        字段同 :class:`CodegraphListEntry`），解析失败或本期不结构化时含
        ``raw``（原始正文全文）。若剥离出 vendor 提示 banner，额外含 ``notice``。

    异常:
        无。任何解析异常都被归一化为全文降级，保证工具链不因展示解析失败而中断。

    副作用:
        解析失败时写一条 ``warn`` 日志（含 tool 名与原文前 200 字符），
        ``trace_id`` 由日志子系统 filter 自动回填。
    """

    parsed = _split_notice(text)
    payload: dict[str, object] = {"tool": tool}
    if parsed.notice:
        payload["notice"] = parsed.notice

    parser = _PARSERS.get(tool)
    if parser is None:
        # explore 本期不结构化，未知工具同样按全文透传，属预期路径，不告警。
        payload["raw"] = parsed.body
        return payload

    try:
        entries = parser(parsed.lines)
    except (ValueError, IndexError, AttributeError) as exc:
        # 正则/切片类异常不应让工具调用失败：降级全文并留下可定位日志。
        log.warning(
            "codegraph_result_parse_failed",
            extra={
                "msg": "CodeGraph 结果解析异常，降级为全文透传",
                "data": {
                    "tool": tool,
                    "error": f"{type(exc).__name__}: {exc}",
                    "raw_preview": parsed.body[:_RAW_LOG_PREVIEW_CHARS],
                },
            },
        )
        payload["raw"] = parsed.body
        return payload

    if not entries:
        log.warning(
            "codegraph_result_parse_empty",
            extra={
                "msg": "CodeGraph 结果未解析出任何条目，降级为全文透传",
                "data": {
                    "tool": tool,
                    "raw_preview": parsed.body[:_RAW_LOG_PREVIEW_CHARS],
                },
            },
        )
        payload["raw"] = parsed.body
        return payload

    payload["items"] = [asdict(entry) for entry in entries]
    return payload


def _split_notice(text: str) -> _ParsedText:
    """剥离 vendor 的「索引降级 / 陈旧」提示行，分离出正文与提示。

    仅剥离**正文起始处**连续的提示行：提示 banner 由 vendor 前置插入，正文中间
    出现的告警符号（如 blast radius 里的 ``⚠️ no covering tests``）属结果内容，
    不应被误剥离。

    参数:
        text: vendor 返回的原始文本。

    返回:
        :class:`_ParsedText`：``body`` 为剥离后的正文，``notice`` 为提示合并文本，
        ``lines`` 为正文按行切分结果（供各 parser 复用，避免重复 split）。

    异常:
        无。

    副作用:
        无。
    """

    lines = text.splitlines()
    notice_lines: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            # 提示 banner 尚未结束前的空行随之跳过；未遇到提示则空行留给正文。
            if notice_lines:
                index += 1
                continue
            break
        if not _NOTICE_LINE_PATTERN.match(line):
            break
        notice_lines.append(line.strip())
        index += 1

    body_lines = lines[index:]
    return _ParsedText(
        body="\n".join(body_lines).strip(),
        notice="\n".join(notice_lines),
        lines=body_lines,
    )


def parse_search(lines: list[str]) -> list[CodegraphListEntry]:
    """解析 ``codegraph_search`` 输出（每个结果为「标题 / 位置 / 签名」三行块）。

    识别形如::

        **resolve_node_binary** (function)
        apps/backend/app/codegraph/node_resolver.py:48
        `() -> Path`

    **标题行必须紧跟位置行**才视为一个结果块。这不是可选优化：当被索引的符号自身
    是多行字符串常量（如本仓库的测试 fixture）时，vendor 会把跨行签名整段回显，
    签名内部可能出现与标题同形的行（`` **x** (function) ``）。若把裸标题也算作结果，
    这些签名内部行会被误判为新符号，产出无路径的脏条目。因此签名块整体跳过，
    直到下一个「标题 + 位置」组合出现。

    参数:
        lines: 已剥离提示 banner 的正文行列表。

    返回:
        结构化列表项；无任何合法结果块时返回空列表（由调用方降级为全文）。

    异常:
        无。

    副作用:
        无。
    """

    entries: list[CodegraphListEntry] = []
    index = 0
    total = len(lines)
    while index < total:
        heading = _SYMBOL_HEADING_PATTERN.match(lines[index].strip())
        if heading is None:
            index += 1
            continue
        # 标题必须紧跟位置行；否则该行只是签名正文里的同形文本，跳过。
        if index + 1 >= total:
            break
        location = _LOCATION_LINE_PATTERN.match(lines[index + 1].strip())
        if location is None:
            index += 1
            continue
        index += 2
        signature, index = _consume_signature(lines, index)
        entries.append(
            CodegraphListEntry(
                name=heading.group("name"),
                kind=heading.group("kind"),
                filePath=location.group("file_path"),
                lineNumber=int(location.group("line")),
                signature=signature,
            )
        )
    return entries


def _consume_signature(lines: list[str], index: int) -> tuple[str, int]:
    """吸收位置行之后可选的签名块（支持反引号包裹的多行签名）。

    单行签名形如 `` `() -> Path` ``；多行签名以 `` ` `` 开始、以 `` ` `` 结束，
    中间是原样回显的源码。多行签名折叠为单行摘要（首行 + 省略号），避免把整段源码
    塞进列表条目。

    参数:
        lines: 正文行列表。
        index: 位置行之后的下标。

    返回:
        ``(签名文本, 新下标)``；无签名块时返回 ``("", index)``。

    异常:
        无。

    副作用:
        无。
    """

    if index >= len(lines):
        return "", index
    stripped = lines[index].strip()
    if not stripped.startswith("`"):
        return "", index
    single_line = _SIGNATURE_LINE_PATTERN.match(stripped)
    if single_line is not None:
        return single_line.group("signature"), index + 1

    # 多行签名：从起始行推进到收尾的反引号行，整体折叠为首行摘要。
    first_line = stripped.lstrip("`").strip()
    cursor = index + 1
    while cursor < len(lines) and not lines[cursor].rstrip().endswith("`"):
        cursor += 1
    # cursor 停在收尾行（或越界）；无论是否找到收尾都从其后继续，避免死循环。
    return f"{first_line} …", min(cursor + 1, len(lines))


def parse_node(lines: list[str]) -> list[CodegraphListEntry]:
    """解析 ``codegraph_node`` 输出（符号自身 + Calls→ / Called by← 两条 trail）。

    首条为符号自身（取 ``**Location:**`` / ``**Signature:**``），其后展开
    ``**Calls →**`` 与 ``**Called by ←**`` 行内以 ``, `` 分隔的条目，
    分别标注 ``edge="calls"`` / ``edge="called by"``。

    参数:
        lines: 已剥离提示 banner 的正文行列表。

    返回:
        结构化列表项；未识别到符号标题且无 trail 条目时返回空列表。

    异常:
        无。

    副作用:
        无。
    """

    entries: list[CodegraphListEntry] = []
    head_name = ""
    head_kind = ""
    head_path = ""
    head_line: int | None = None
    head_signature = ""
    trail_entries: list[CodegraphListEntry] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        heading = _SYMBOL_HEADING_PATTERN.match(line)
        if heading is not None and not head_name:
            head_name = heading.group("name")
            head_kind = heading.group("kind")
            continue
        location = _NODE_LOCATION_PATTERN.match(line)
        if location is not None:
            head_path = location.group("file_path")
            head_line = int(location.group("line"))
            continue
        signature_match = _NODE_SIGNATURE_PATTERN.match(line)
        if signature_match is not None:
            head_signature = signature_match.group("signature")
            continue
        trail = _NODE_TRAIL_PATTERN.match(line)
        if trail is not None:
            edge = "calls" if trail.group("label") == "Calls" else "called by"
            trail_entries.extend(_parse_trail_body(trail.group("body"), edge))

    if head_name:
        entries.append(
            CodegraphListEntry(
                name=head_name,
                kind=head_kind,
                filePath=head_path,
                lineNumber=head_line,
                signature=head_signature,
            )
        )
    entries.extend(trail_entries)
    return entries


def _parse_trail_body(body: str, edge: str) -> list[CodegraphListEntry]:
    """解析 node trail 行内的 ``name (path:line), name (path:line)`` 序列。

    参数:
        body: trail 标签之后的正文部分。
        edge: 该 trail 对应的边标签（``calls`` / ``called by``）。

    返回:
        结构化列表项；无法匹配 ``name (path:line)`` 的片段被跳过。

    异常:
        无。

    副作用:
        无。
    """

    entries: list[CodegraphListEntry] = []
    for chunk in body.split("), "):
        candidate = chunk.strip()
        if not candidate:
            continue
        # split("), ") 会吃掉除末项外的右括号，补回后统一匹配。
        if not candidate.endswith(")"):
            candidate = f"{candidate})"
        match = _TRAIL_ENTRY_PATTERN.match(candidate)
        if match is None:
            continue
        entries.append(
            CodegraphListEntry(
                name=match.group("name"),
                filePath=match.group("file_path"),
                lineNumber=int(match.group("line")),
                edge=edge,
            )
        )
    return entries


def parse_call_edges(lines: list[str]) -> list[CodegraphListEntry]:
    """解析 ``codegraph_callers`` / ``codegraph_callees`` 输出（同构单行列表）。

    识别形如（``— via edge`` 后缀可选）::

        - _spawn (method) - apps/backend/app/codegraph/supervisor.py:238
        - __init__.py (file) - apps/backend/app/codegraph/__init__.py:1 — via import

    两个工具输出结构完全一致，共用本 parser，避免重复实现。

    参数:
        lines: 已剥离提示 banner 的正文行列表。

    返回:
        结构化列表项；无匹配行时返回空列表。

    异常:
        无。

    副作用:
        无。
    """

    entries: list[CodegraphListEntry] = []
    for raw_line in lines:
        match = _CALL_EDGE_ENTRY_PATTERN.match(raw_line.strip())
        if match is None:
            continue
        edge = match.group("edge")
        entries.append(
            CodegraphListEntry(
                name=match.group("name"),
                kind=match.group("kind"),
                filePath=match.group("file_path"),
                lineNumber=int(match.group("line")),
                edge=edge.strip() if edge else "",
            )
        )
    return entries


def parse_impact(lines: list[str]) -> list[CodegraphListEntry]:
    """解析 ``codegraph_impact`` 输出（按 ``**file:**`` 分节 + 逗号分隔符号）。

    识别形如::

        **apps/backend/app/codegraph/supervisor.py:**
        _spawn:238, start:118, supervisor.py:1

    本期扁平化输出（每个受影响符号一条，携带所属文件路径），分组展示后续再做。

    参数:
        lines: 已剥离提示 banner 的正文行列表。

    返回:
        结构化列表项；无任何文件分节时返回空列表。

    异常:
        无。

    副作用:
        无。
    """

    entries: list[CodegraphListEntry] = []
    current_file = ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        heading = _IMPACT_FILE_HEADING_PATTERN.match(line)
        if heading is not None:
            current_file = heading.group("file_path").strip()
            continue
        if not current_file or line.startswith("**"):
            # 尚未进入任何文件分节（如首行标题），或是其他加粗标题，跳过。
            continue
        for chunk in line.split(","):
            symbol = chunk.strip()
            if not symbol:
                continue
            match = _IMPACT_SYMBOL_PATTERN.match(symbol)
            if match is None:
                continue
            entries.append(
                CodegraphListEntry(
                    name=match.group("name"),
                    filePath=current_file,
                    lineNumber=int(match.group("line")),
                )
            )
    return entries


# 工具名 → parser 的分派表。explore 刻意缺席：本期不结构化，走全文降级。
_PARSERS: dict[str, Callable[[list[str]], list[CodegraphListEntry]]] = {
    "codegraph_search": parse_search,
    "codegraph_node": parse_node,
    "codegraph_callers": parse_call_edges,
    "codegraph_callees": parse_call_edges,
    "codegraph_impact": parse_impact,
}
