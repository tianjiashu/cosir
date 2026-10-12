"""将 Git 统一 diff 解析为「修改已有文件」的差异块。

本模块只接受每个文件段仅含单个文件的 Git 统一 diff。在调用方解析路径或触碰工作区之前，
它先拒绝文件创建、删除、重命名、复制、二进制以及模式变更等元数据；对于声明行数与自身
正文不符的 hunk 头，它会重写行数并把每次重写作为备注回报，而不是让整个调用失败。
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

from unidiff import PatchSet
from unidiff.constants import RE_HUNK_HEADER
from unidiff.errors import UnidiffParseError


@dataclass(frozen=True)
class HunkLine:
    """统一 diff 差异块中的一行：上下文、删除或新增。"""

    prefix: str
    content: str


@dataclass(frozen=True)
class Hunk:
    """一个已解析的文本差异块，保留原始行用于内容匹配。"""

    context_hint: str | None = None
    lines: list[HunkLine] = field(default_factory=list)
    source_start: int = 0
    target_start: int = 0
    source_length: int = 0
    target_length: int = 0
    new_no_newline_at_eof: bool = False


@dataclass(frozen=True)
class PatchOperation:
    """对某个工作区相对路径的已有文件的一次纯内容更新。"""

    file_path: str
    hunks: list[Hunk]


def _split_sections(patch: str) -> tuple[list[str], str | None]:
    """按 Git 文件头切分补丁，并拒绝含糊的前导内容。"""

    lines = patch.splitlines(keepends=True)
    for marker in ("*** Begin Patch", "*** End Patch"):
        if any(line.strip() == marker for line in lines):
            return [], f"patch contains unsupported wrapper marker {marker!r}"
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    if not starts or starts[0] != 0:
        return [], "expected a Git 'diff --git' header at the start of the patch"
    if any(
        line.startswith(("diff --cc ", "diff --combined "))
        for line in lines
        if line.startswith("diff --") and not line.startswith("diff --git ")
    ):
        return [], "combined diffs are not supported"
    return [
        "".join(lines[start : starts[index + 1] if index + 1 < len(starts) else len(lines)])
        for index, start in enumerate(starts)
    ], None


def _decode_git_path(value: str, prefix: str) -> str:
    """解码 Git 的 C 引号转义路径，返回其工作区相对路径。"""

    value = value.strip()
    if value.startswith('"'):
        if not value.endswith('"') or len(value) < 2:
            raise ValueError("malformed quoted path")
        try:
            raw = codecs.escape_decode(value[1:-1].encode("utf-8"))[0]
            value = raw.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("quoted path is not valid UTF-8") from exc
    if not value.startswith(prefix):
        raise ValueError(f"expected a {prefix!r} workspace-relative path")
    path = value[len(prefix) :]
    if (
        not path
        or "\x00" in path
        or "\\" in path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("path must be a normalized workspace-relative file path")
    return path


def _diff_header_paths(section: str, section_index: int) -> tuple[str, str, str | None]:
    """读取 Git 头部的两条路径，支持含空格的 C 引号文件名。"""

    header = section.splitlines()[0]
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[^ \t]+', header[len("diff --git ") :])
    if len(tokens) != 2:
        return "", "", f"file section {section_index + 1} has a malformed Git header"
    try:
        source = _decode_git_path(tokens[0], "a/")
        target = _decode_git_path(tokens[1], "b/")
    except ValueError as exc:
        return "", "", f"file section {section_index + 1}: {exc}"
    if source != target:
        return "", "", "each diff section must modify one existing file without renaming it"
    return source, target, None


def _validate_section_headers(section: str) -> str | None:
    """校验 Git 元数据以及必需的统一 diff 文件头。"""

    lines = section.splitlines()
    if not lines or not lines[0].startswith("diff --git "):
        return "each file section must begin with 'diff --git'"
    if any(
        line.startswith(
            (
                "new file mode ",
                "deleted file mode ",
                "rename from ",
                "rename to ",
                "copy from ",
                "copy to ",
                "old mode ",
                "new mode ",
                "GIT binary patch",
                "Binary files ",
            )
        )
        for line in lines
    ):
        return (
            "file creation, deletion, move, copy, mode changes, and binary patches "
            "are not supported"
        )
    if any(line.startswith("diff --") and not line.startswith("diff --git ") for line in lines):
        return "only Git file diffs are supported; combined diffs are not supported"

    malformed_hunk = next(
        (
            line.strip()
            for line in lines
            if line.startswith("@@") and RE_HUNK_HEADER.match(line) is None
        ),
        None,
    )
    if malformed_hunk is not None:
        return (
            f"malformed hunk header {malformed_hunk[:80]!r}; expected "
            "'@@ -start[,count] +start[,count] @@'"
        )

    hunks = [index for index, line in enumerate(lines) if line.startswith("@@ ")]
    first_hunk = hunks[0] if hunks else len(lines)
    old_headers = [
        index for index, line in enumerate(lines[:first_hunk]) if line.startswith("--- ")
    ]
    new_headers = [
        index for index, line in enumerate(lines[:first_hunk]) if line.startswith("+++ ")
    ]
    if len(old_headers) != 1 or len(new_headers) != 1 or not hunks:
        return (
            "each file section must contain exactly one '---', one '+++', and at least one "
            "'@@' hunk"
        )
    if not (1 <= old_headers[0] < new_headers[0] < hunks[0]):
        return "unified-diff file headers must appear before the hunks"

    # Git 的 index 行是唯一被接受的扩展元数据。拒绝未知元数据，
    # 而不是放任解析器静默忽略文件操作。
    for line in lines[1 : old_headers[0]]:
        if line and not line.startswith("index "):
            return f"unsupported Git diff metadata: {line}"
    for line in lines[hunks[0] + 1 :]:
        if line and line[0] not in {" ", "+", "-", "\\", "@"}:
            return "unexpected text outside a unified-diff hunk"
    return None


def _recompute_hunk_counts(section: str, section_index: int) -> tuple[str, list[str]]:
    """重写每个声明行数与所引正文不符的 ``@@`` 头。

    一个 hunk 的正文，是其 ``@@`` 头与下一个 ``@@`` 头（或段末）之间的所有行，因为那正是调用方
    作为本块写下的文本。尾部空行会被丢弃而不计入：它们只用于分隔文件段、分隔 hunk 或终止补丁，
    不属于任何 hunk。其余计数规则与 ``unidiff`` 一致：空正文行在两侧都计为上下文，省略的计数
    表示一行，``\\ No newline at end of file`` 标记两侧都不计入。

    「修复」而非「拒绝」是刻意为之。应用层通过 ``fuzzy_match`` 按上下文文本匹配 hunk，从不读取
    计数，因此一个计错的数头在数值一致后是无害的；而 ``unidiff`` 会按声明计数消费 hunk，计数不符
    会让文件段错位，表现为毫不相干的「意外的 hunk」/「hunk 短于预期」错误。重写数头使补丁保持
    可应用，并让调用方回报一条诚实的备注，而不是泛起不相关的解析失败。

    参数:
        section: 一个 Git 文件段，以其 ``diff --git`` 头起始。
        section_index: 从零开始的段序号，用于在备注中定位 hunk。

    返回:
        ``(section_text, notes)``：所有不匹配的 ``@@`` 头都被重写（起始行与段文本保留），
        外加每次重写对应的一条面向模型的备注，标注位置、原样写入的头部，以及新旧计数。
        头部本来一致的段原样返回且不含备注。

    异常:
        无。

    副作用:
        无（纯字符串分析，不访问文件系统）。
    """

    lines = section.splitlines(keepends=True)
    headers: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        matched = RE_HUNK_HEADER.match(line)
        if matched:
            headers.append((index, matched))
    if not headers:
        return section, []
    rewritten = list(lines)
    notes: list[str] = []
    for position, (start, header) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        body = [line.rstrip("\r\n") for line in lines[start + 1 : end]]
        while body and not body[-1]:
            body.pop()
        source_lines = sum(1 for line in body if line[:1] in {" ", "", "-"})
        target_lines = sum(1 for line in body if line[:1] in {" ", "", "+"})
        declared_source = int(header.group(2) or 1)
        declared_target = int(header.group(4) or 1)
        if declared_source == source_lines and declared_target == target_lines:
            continue
        ending = "\r\n" if lines[start].endswith("\r\n") else "\n"
        rewritten[start] = (
            f"@@ -{header.group(1)},{source_lines} +{header.group(3)},{target_lines} @@"
            f"{header.group(5)}{ending}"
        )
        notes.append(
            f"file section {section_index + 1} hunk {position + 1}: '{lines[start].rstrip()}' "
            f"declared source={declared_source} target={declared_target} but body has "
            f"source={source_lines} target={target_lines}; counts recomputed from the body and "
            f"applied as written"
        )
    return "".join(rewritten), notes


def _repair_hunk_counts(patch: str) -> tuple[str, list[str]]:
    """解析前对 ``patch`` 的每个文件段做归一化。

    参数:
        patch: 调用方提交的 Git 统一 diff 文本。

    返回:
        ``(patch_text, notes)``。当 diff 没有可识别的 Git 文件段（解析器会自行报告该形态）、
        没有 hunk 头需要重写，或重写不会改变任何内容时，文本原样返回且不含备注。

    异常:
        无。

    副作用:
        无（纯字符串分析，不访问文件系统）。
    """

    if not isinstance(patch, str) or not patch.strip():
        return patch, []
    normalized_input = patch if patch.endswith("\n") else patch + "\n"
    sections, error = _split_sections(normalized_input)
    if error:
        return patch, []
    repaired_sections: list[str] = []
    notes: list[str] = []
    for index, section in enumerate(sections):
        section_text, section_notes = _recompute_hunk_counts(section, index)
        repaired_sections.append(section_text)
        notes.extend(section_notes)
    if not notes:
        return patch, []
    return "".join(repaired_sections), notes


def _normalize_parser_paths(
    sections: list[str],
) -> tuple[list[str], list[str], str | None]:
    """在校验头部之后，把可变的 Git 路径替换为安全的解析器标签。"""

    normalized: list[str] = []
    paths: list[str] = []
    for index, section in enumerate(sections):
        source, _, error = _diff_header_paths(section, index)
        if error:
            return [], [], error
        lines = section.splitlines(keepends=True)
        old_index = next(i for i, line in enumerate(lines) if line.startswith("--- "))
        new_index = next(i for i, line in enumerate(lines) if line.startswith("+++ "))
        old_value = lines[old_index][4:].rstrip("\r\n").split("\t", 1)[0]
        new_value = lines[new_index][4:].rstrip("\r\n").split("\t", 1)[0]
        try:
            old_path = _decode_git_path(old_value, "a/")
            new_path = _decode_git_path(new_value, "b/")
        except ValueError as exc:
            return [], [], f"file section {index + 1}: {exc}"
        if old_path != source or new_path != source:
            return [], [], "the Git, '---', and '+++' headers must name the same existing file"

        label = f"codex_patch_file_{index}"
        lines[0] = f"diff --git a/{label} b/{label}\n"
        lines[old_index] = f"--- a/{label}\n"
        lines[new_index] = f"+++ b/{label}\n"
        normalized.append("".join(lines))
        paths.append(source)
    return normalized, paths, None


@dataclass(frozen=True)
class PatchParseOutcome:
    """一次 Git 统一 diff 解析的结果，包含任何 hunk 头计数修复。

    属性:
        operations: 被接受补丁的 UPDATE 操作；``error`` 有值时为空。
        error: 面向模型的拒绝消息；diff 被接受时为 None。
        count_repairs: 每条「声明计数被按正文重算」的 hunk 对应一条备注。
            仅对接受的补丁回报；被修复的头部仍表示补丁应用其正文所写的全部内容。
    """

    operations: list[PatchOperation]
    error: str | None
    count_repairs: list[str]


def parse_git_unified_diff(patch: str) -> tuple[list[PatchOperation], str | None]:
    """解析 Git 统一 diff，不进行文件系统访问。

    面向「只需操作列表或拒绝消息」的调用方的便捷入口；若还需 hunk 计数修复备注，请用
    :func:`parse_git_unified_diff_detailed`。两个入口都会修复（而非拒绝）与自身正文不符的
    hunk 头（见 :func:`_recompute_hunk_counts`）。

    参数:
        patch: Git 统一 diff 文本。

    返回:
        diff 被接受时返回 ``(operations, None)``；否则返回 ``([], message)``，附带面向模型的诊断。
        只返回 UPDATE 操作；工作区边界检查与文件校验仍由调用方负责。

    异常:
        无。

    副作用:
        无。
    """

    outcome = parse_git_unified_diff_detailed(patch)
    return outcome.operations, outcome.error


def parse_git_unified_diff_detailed(patch: str) -> PatchParseOutcome:
    """解析 Git 统一 diff，回报每条「计数被修复」的 hunk 头。

    补丁先经 :func:`_repair_hunk_counts` 归一化，因此与所引正文不符的头部不再导致调用失败：
    计数按正文重写，并把重写结果回报给调用方。

    参数:
        patch: Git 统一 diff 文本。

    返回:
        :class:`PatchParseOutcome`。被拒绝的补丁、以及头部本就与正文一致的补丁，其 ``count_repairs`` 为空。

    异常:
        无。

    副作用:
        无。
    """

    repaired_patch, count_repairs = _repair_hunk_counts(patch)
    operations, error = _parse_sections(repaired_patch)
    return PatchParseOutcome(operations, error, count_repairs if error is None else [])


def _parse_sections(patch: str) -> tuple[list[PatchOperation], str | None]:
    """校验文件头，并把一个「计数已归一化」的 Git 统一 diff 转换为操作列表。

    参数:
        patch: ``@@`` 头已与正文一致的 Git 统一 diff 文本。

    返回:
        diff 被接受时返回 ``(operations, None)``；否则返回 ``([], message)``，附带面向模型的诊断。
        段与文件头的错误优先于后续所有阶段，因此一个在多处出错的补丁会报告最早的问题。

    异常:
        无。

    副作用:
        无。
    """

    if not isinstance(patch, str) or not patch.strip():
        return [], "patch must be a non-empty Git unified diff"
    normalized_input = patch if patch.endswith("\n") else patch + "\n"
    sections, error = _split_sections(normalized_input)
    if error:
        return [], error
    for section in sections:
        error = _validate_section_headers(section)
        if error:
            return [], error
    normalized_sections, paths, error = _normalize_parser_paths(sections)
    if error:
        return [], error
    try:
        parsed = PatchSet.from_string("".join(normalized_sections))
    except (UnidiffParseError, ValueError, IndexError) as exc:
        return [], f"invalid unified diff: {exc}"
    if len(parsed) != len(sections):
        return [], "unified diff contains an incomplete or ambiguous file section"

    operations: list[PatchOperation] = []
    seen_paths: set[str] = set()
    for file, path in zip(parsed, paths, strict=True):
        if file.is_binary_file or not file:
            return [], "binary patches and file sections without text hunks are not supported"
        if file.source_file == "/dev/null" or file.target_file == "/dev/null":
            return [], "file creation and deletion are not supported by apply_patch"
        if path in seen_paths:
            return [], f"duplicate diff section for {path!r}"
        seen_paths.add(path)

        converted_hunks: list[Hunk] = []
        for parsed_hunk in file:
            converted: list[HunkLine] = []
            new_no_newline = False
            previous_line_type: str | None = None
            for line in parsed_hunk:
                if line.line_type == "\\":
                    if previous_line_type in {" ", "+"}:
                        new_no_newline = True
                elif line.line_type in {" ", "+", "-"}:
                    value = line.value[:-1] if line.value.endswith("\n") else line.value
                    converted.append(HunkLine(line.line_type, value))
                    previous_line_type = line.line_type
            if not converted:
                return [], f"{path}: hunk has no context or changed lines"
            if not any(line.prefix in {"+", "-"} for line in converted):
                return [], f"{path}: hunk does not change file contents"
            converted_hunks.append(
                Hunk(
                    context_hint=parsed_hunk.section_header or None,
                    lines=converted,
                    source_start=parsed_hunk.source_start,
                    target_start=parsed_hunk.target_start,
                    source_length=parsed_hunk.source_length,
                    target_length=parsed_hunk.target_length,
                    new_no_newline_at_eof=new_no_newline,
                )
            )
        operations.append(PatchOperation(file_path=path, hunks=converted_hunks))

    if not operations:
        return [], "patch contains no file updates"
    return operations, None
