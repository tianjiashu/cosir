"""模糊匹配模块：多策略查找替换（移植自 Hermes ``tools/fuzzy_match.py``）。

实现一条由 9 个策略组成的匹配链，用于稳健地查找并替换文本，容忍 LLM 生成
代码中常见的空白、缩进与转义差异。9 个策略（参考 OpenCode）按顺序尝试：
exact / line_trimmed / whitespace_normalized / indentation_flexible /
escape_normalized / trimmed_boundary / unicode_normalized / block_anchor /
context_aware。

多命中匹配通过 ``replace_all`` 控制。本模块为纯逻辑，不读写文件。

对外导出：
- ``fuzzy_find_and_replace``：主入口，返回 ``(新内容, 命中数, 策略名, 错误)``。
- ``format_no_match_hint``：独立模块级函数，不被 ``fuzzy_find_and_replace``
  调用；由 patch 工具在调用点显式拼装 "Did you mean" 提示。
"""

import re
from collections.abc import Callable
from difflib import SequenceMatcher

UNICODE_MAP: dict[str, str] = {
    "\u201c": '"',
    "\u201d": '"',  # smart double quotes
    "\u2018": "'",
    "\u2019": "'",  # smart single quotes
    "\u2014": "--",
    "\u2013": "-",  # em/en dashes
    "\u2026": "...",
    "\u00a0": " ",  # ellipsis and non-breaking space
}


def _unicode_normalize(text: str) -> str:
    """把 Unicode 字符归一化为标准 ASCII 等价字符。

    参数:
        text: 待归一化文本。

    返回:
        归一化后的文本。

    异常:
        无。

    副作用:
        无。
    """

    for char, repl in UNICODE_MAP.items():
        text = text.replace(char, repl)
    return text


def fuzzy_find_and_replace(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> tuple[str, int, str | None, str | None]:
    """用一条逐渐模糊的匹配策略链查找并替换文本。

    参数:
        content: 待搜索的源文件内容。
        old_string: 待查找的文本。
        new_string: 替换文本。
        replace_all: 为 True 替换所有命中；为 False 要求唯一命中。

    返回:
        ``(新内容, 命中数, 命中策略名, 错误信息)``；成功时错误为 None，
        失败时新内容为原内容、命中数为 0、策略名为 None。

    异常:
        不向上抛出；所有失败路径都收敛为错误字符串返回。

    副作用:
        无（纯函数）。
    """

    if not old_string:
        return content, 0, None, "old_string cannot be empty"

    if old_string == new_string:
        return content, 0, None, "old_string and new_string are identical"

    strategies: list[tuple[str, Callable[[str, str], list[tuple[int, int]]]]] = [
        ("exact", _strategy_exact),
        ("line_trimmed", _strategy_line_trimmed),
        ("whitespace_normalized", _strategy_whitespace_normalized),
        ("indentation_flexible", _strategy_indentation_flexible),
        ("escape_normalized", _strategy_escape_normalized),
        ("trimmed_boundary", _strategy_trimmed_boundary),
        ("unicode_normalized", _strategy_unicode_normalized),
        ("block_anchor", _strategy_block_anchor),
        ("context_aware", _strategy_context_aware),
    ]

    for strategy_name, strategy_fn in strategies:
        matches = strategy_fn(content, old_string)

        if matches:
            if len(matches) > 1 and not replace_all:
                return (
                    content,
                    0,
                    None,
                    (
                        f"Found {len(matches)} matches for old_string. "
                        f"Provide more context to make it unique, or use replace_all=True."
                    ),
                )

            if strategy_name != "exact":
                drift_err = _detect_escape_drift(content, matches, old_string, new_string)
                if drift_err:
                    return content, 0, None, drift_err

            effective_new = _maybe_unescape_new_string(new_string, content, matches)
            if strategy_name == "unicode_normalized":
                effective_new = _preserve_unicode_in_replacement(
                    content, matches, old_string, effective_new
                )
            new_content = _apply_replacements(
                content,
                matches,
                effective_new,
                old_string=old_string if strategy_name != "exact" else None,
            )
            return new_content, len(matches), strategy_name, None

    return content, 0, None, "Could not find a match for old_string in the file"


def _detect_escape_drift(
    content: str,
    matches: list[tuple[int, int]],
    old_string: str,
    new_string: str,
) -> str | None:
    """检测 new_string 中的工具调用转义漂移产物。

    参数:
        content: 源文件内容。
        matches: 命中区域列表。
        old_string: 原始查找文本。
        new_string: 替换文本。

    返回:
        检测到漂移时返回错误字符串，否则返回 None。

    异常:
        无。

    副作用:
        无。
    """

    if "\\'" not in new_string and '\\"' not in new_string:
        return None

    matched_regions = "".join(content[start:end] for start, end in matches)

    for suspect in ("\\'", '\\"'):
        if suspect in new_string and suspect in old_string and suspect not in matched_regions:
            plain = suspect[1]
            return (
                f"Escape-drift detected: old_string and new_string contain "
                f"the literal sequence {suspect!r} but the matched region of "
                f"the file does not. This is almost always a tool-call "
                f"serialization artifact where an apostrophe or quote got "
                f"prefixed with a spurious backslash. Re-read the file with "
                f"read_file and pass old_string/new_string without "
                f"backslash-escaping {plain!r} characters."
            )
    return None


def _leading_whitespace(line: str) -> str:
    """返回一行的前导空白前缀（空格/制表符）。

    参数:
        line: 待检查行。

    返回:
        前导空白字符串。

    异常:
        无。

    副作用:
        无。
    """

    i = 0
    while i < len(line) and line[i] in (" ", "\t"):
        i += 1
    return line[:i]


def _first_meaningful_line(text: str) -> str | None:
    """返回文本中第一个非空内容行。

    参数:
        text: 待检查文本。

    返回:
        第一个有意义行；文本为空或全空白时返回 None。

    异常:
        无。

    副作用:
        无。
    """

    for line in text.split("\n"):
        if line.strip():
            return line
    return None


def _reindent_replacement(file_region: str, old_string: str, new_string: str) -> str:
    """把 new_string 的缩进调整为与文件实际缩进一致。

    参数:
        file_region: 命中区域文本。
        old_string: 原始查找文本（LLM 视角缩进）。
        new_string: 替换文本（LLM 视角缩进）。

    返回:
        缩进对齐后的替换文本。

    异常:
        无。

    副作用:
        无。
    """

    if not new_string:
        return new_string

    old_first = _first_meaningful_line(old_string)
    file_first = _first_meaningful_line(file_region)
    if old_first is None or file_first is None:
        return new_string

    old_indent = _leading_whitespace(old_first)
    file_indent = _leading_whitespace(file_first)

    if old_indent == file_indent:
        return new_string

    out_lines: list[str] = []
    for line in new_string.split("\n"):
        if not line.strip():
            out_lines.append(line)
            continue
        line_indent = _leading_whitespace(line)
        if line_indent.startswith(old_indent):
            remainder = line[len(old_indent) :]
            out_lines.append(file_indent + remainder)
        else:
            out_lines.append(file_indent + line.lstrip(" \t"))
    return "\n".join(out_lines)


def _maybe_unescape_new_string(
    new_string: str,
    content: str,
    matches: list[tuple[int, int]],
) -> str:
    """按命中区域是否含真实控制字符，条件性反转义 new_string 中的 ``\\t``/``\\r``。

    参数:
        new_string: 替换文本。
        content: 源文件内容。
        matches: 命中区域列表。

    返回:
        处理后的替换文本。

    异常:
        无。

    副作用:
        无。
    """

    if "\\t" not in new_string and "\\r" not in new_string:
        return new_string

    matched_regions = "".join(content[start:end] for start, end in matches)
    out = new_string
    if "\\t" in out and "\t" in matched_regions:
        out = out.replace("\\t", "\t")
    if "\\r" in out and "\r" in matched_regions:
        out = out.replace("\\r", "\r")
    return out


def _preserve_unicode_in_replacement(
    content: str,
    matches: list[tuple[int, int]],
    old_string: str,
    new_string: str,
) -> str:
    """在 unicode_normalized 命中时，保留文件原始 Unicode 字符于未改动片段。

    参数:
        content: 源文件内容。
        matches: 命中区域列表。
        old_string: 原始查找文本。
        new_string: 替换文本。

    返回:
        Unicode 对齐后的替换文本。

    异常:
        无。

    副作用:
        无。
    """

    file_region = "".join(content[start:end] for start, end in matches)

    norm_old = _unicode_normalize(old_string)
    norm_file = _unicode_normalize(file_region)

    if norm_old != norm_file:
        return new_string

    file_orig_to_norm = _build_orig_to_norm_map(file_region)
    file_norm_to_orig: dict[int, int] = {}
    for orig_pos, np in enumerate(file_orig_to_norm[:-1]):
        if np not in file_norm_to_orig:
            file_norm_to_orig[np] = orig_pos

    sm = SequenceMatcher(None, norm_old, new_string)
    opcodes = sm.get_opcodes()

    result_parts: list[str] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            orig_start = file_norm_to_orig.get(i1, 0)
            orig_end = orig_start
            while orig_end < len(file_region) and file_orig_to_norm[orig_end] < i2:
                orig_end += 1
            result_parts.append(file_region[orig_start:orig_end])
        elif tag == "replace":
            result_parts.append(new_string[j1:j2])
        elif tag == "delete":
            pass
        elif tag == "insert":
            result_parts.append(new_string[j1:j2])

    return "".join(result_parts)


def _apply_replacements(
    content: str,
    matches: list[tuple[int, int]],
    new_string: str,
    old_string: str | None = None,
) -> str:
    """在给定位置应用替换。

    参数:
        content: 原内容。
        matches: ``(start, end)`` 命中位置列表。
        new_string: 替换文本。
        old_string: 非 None 时表示来自非 exact 模糊策略，先按文件实际缩进重排 new_string。

    返回:
        应用替换后的内容。

    异常:
        无。

    副作用:
        无。
    """

    sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)

    result = content
    for start, end in sorted_matches:
        if old_string is not None:
            file_region = content[start:end]
            adjusted = _reindent_replacement(file_region, old_string, new_string)
        else:
            adjusted = new_string
        result = result[:start] + adjusted + result[end:]

    return result


# =============================================================================
# 匹配策略
# =============================================================================


def _strategy_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    """策略 1：精确字符串匹配。

    参数:
        content: 源内容。
        pattern: 查找模式。

    返回:
        命中 ``(start, end)`` 位置列表（非重叠）。

    异常:
        无。

    副作用:
        无。
    """

    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + len(pattern)
    return matches


def _strategy_line_trimmed(content: str, pattern: str) -> list[tuple[int, int]]:
    """策略 2：逐行去首尾空白后匹配。

    参数:
        content: 源内容。
        pattern: 查找模式。

    返回:
        命中 ``(start, end)`` 位置列表（已映射回原内容）。

    异常:
        无。

    副作用:
        无。
    """

    pattern_lines = [line.strip() for line in pattern.split("\n")]
    pattern_normalized = "\n".join(pattern_lines)

    content_lines = content.split("\n")
    content_normalized_lines = [line.strip() for line in content_lines]

    return _find_normalized_matches(
        content,
        content_lines,
        content_normalized_lines,
        pattern,
        pattern_normalized,
    )


def _strategy_whitespace_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    """策略 3：多个空白折叠为单空格后匹配。

    参数:
        content: 源内容。
        pattern: 查找模式。

    返回:
        命中 ``(start, end)`` 位置列表（已映射回原内容）。

    异常:
        无。

    副作用:
        无。
    """

    def normalize(s: str) -> str:
        return re.sub(r"[ \t]+", " ", s)

    pattern_normalized = normalize(pattern)
    content_normalized = normalize(content)

    matches_in_normalized = _strategy_exact(content_normalized, pattern_normalized)
    if not matches_in_normalized:
        return []

    return _map_normalized_positions(content, content_normalized, matches_in_normalized)


def _strategy_indentation_flexible(content: str, pattern: str) -> list[tuple[int, int]]:
    """策略 4：忽略所有行首缩进后匹配。

    参数:
        content: 源内容。
        pattern: 查找模式。

    返回:
        命中 ``(start, end)`` 位置列表（已映射回原内容）。

    异常:
        无。

    副作用:
        无。
    """

    content_lines = content.split("\n")
    content_stripped_lines = [line.lstrip() for line in content_lines]
    pattern_lines = [line.lstrip() for line in pattern.split("\n")]

    return _find_normalized_matches(
        content,
        content_lines,
        content_stripped_lines,
        pattern,
        "\n".join(pattern_lines),
    )


def _strategy_escape_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    """策略 5：把转义序列还原为真实字符后匹配（``\\n``/``\\t``/``\\r``）。

    参数:
        content: 源内容。
        pattern: 查找模式。

    返回:
        命中 ``(start, end)`` 位置列表（无转义可转时返回空）。

    异常:
        无。

    副作用:
        无。
    """

    def unescape(s: str) -> str:
        return s.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")

    pattern_unescaped = unescape(pattern)

    if pattern_unescaped == pattern:
        return []

    return _strategy_exact(content, pattern_unescaped)


def _strategy_trimmed_boundary(content: str, pattern: str) -> list[tuple[int, int]]:
    """策略 6：仅裁剪首尾行空白后匹配。

    参数:
        content: 源内容。
        pattern: 查找模式。

    返回:
        命中 ``(start, end)`` 位置列表（已映射回原内容）。

    异常:
        无。

    副作用:
        无。
    """

    pattern_lines = pattern.split("\n")
    if not pattern_lines:
        return []

    pattern_lines[0] = pattern_lines[0].strip()
    if len(pattern_lines) > 1:
        pattern_lines[-1] = pattern_lines[-1].strip()

    modified_pattern = "\n".join(pattern_lines)
    content_lines = content.split("\n")

    matches: list[tuple[int, int]] = []
    pattern_line_count = len(pattern_lines)

    for i in range(len(content_lines) - pattern_line_count + 1):
        block_lines = content_lines[i : i + pattern_line_count]

        check_lines = block_lines.copy()
        check_lines[0] = check_lines[0].strip()
        if len(check_lines) > 1:
            check_lines[-1] = check_lines[-1].strip()

        if "\n".join(check_lines) == modified_pattern:
            start_pos, end_pos = _calculate_line_positions(
                content_lines, i, i + pattern_line_count, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _build_orig_to_norm_map(original: str) -> list[int]:
    """构建原字符索引到归一化索引的映射表。

    参数:
        original: 原字符串。

    返回:
        长度为 ``len(original)+1`` 的映射列表；``i`` 处为字符 ``i`` 对应的归一化索引。

    异常:
        无。

    副作用:
        无。
    """

    result: list[int] = []
    norm_pos = 0
    for char in original:
        result.append(norm_pos)
        repl = UNICODE_MAP.get(char)
        norm_pos += len(repl) if repl is not None else 1
    result.append(norm_pos)
    return result


def _map_positions_norm_to_orig(
    orig_to_norm: list[int],
    norm_matches: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """把归一化字符串中的 ``(start, end)`` 映射回原字符串位置。

    参数:
        orig_to_norm: 原→归一化索引映射。
        norm_matches: 归一化空间的命中位置。

    返回:
        原字符串空间的命中位置列表。

    异常:
        无。

    副作用:
        无。
    """

    norm_to_orig_start: dict[int, int] = {}
    for orig_pos, norm_pos in enumerate(orig_to_norm[:-1]):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos

    results: list[tuple[int, int]] = []
    orig_len = len(orig_to_norm) - 1

    for norm_start, norm_end in norm_matches:
        if norm_start not in norm_to_orig_start:
            continue
        orig_start = norm_to_orig_start[norm_start]

        orig_end = orig_start
        while orig_end < orig_len and orig_to_norm[orig_end] < norm_end:
            orig_end += 1

        results.append((orig_start, orig_end))

    return results


def _strategy_unicode_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    """策略 7：Unicode 归一化后做 exact / line_trimmed 匹配。

    参数:
        content: 源内容。
        pattern: 查找模式。

    返回:
        命中 ``(start, end)`` 位置列表（已映射回原内容）。

    异常:
        无。

    副作用:
        无。
    """

    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)
    if norm_content == content and norm_pattern == pattern:
        return []

    norm_matches = _strategy_exact(norm_content, norm_pattern)
    if not norm_matches:
        norm_matches = _strategy_line_trimmed(norm_content, norm_pattern)

    if not norm_matches:
        return []

    orig_to_norm = _build_orig_to_norm_map(content)
    return _map_positions_norm_to_orig(orig_to_norm, norm_matches)


def _strategy_block_anchor(content: str, pattern: str) -> list[tuple[int, int]]:
    """策略 8：以首尾行锚定 + 中间相似度匹配。

    参数:
        content: 源内容。
        pattern: 查找模式。

    返回:
        命中 ``(start, end)`` 位置列表（已映射回原内容）。

    异常:
        无。

    副作用:
        无。
    """

    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)

    pattern_lines = norm_pattern.split("\n")
    if len(pattern_lines) < 2:
        return []

    first_line = pattern_lines[0].strip()
    last_line = pattern_lines[-1].strip()

    norm_content_lines = norm_content.split("\n")
    orig_content_lines = content.split("\n")

    pattern_line_count = len(pattern_lines)

    potential_matches: list[int] = []
    for i in range(len(norm_content_lines) - pattern_line_count + 1):
        if (
            norm_content_lines[i].strip() == first_line
            and norm_content_lines[i + pattern_line_count - 1].strip() == last_line
        ):
            potential_matches.append(i)

    matches: list[tuple[int, int]] = []
    candidate_count = len(potential_matches)

    threshold = 0.50 if candidate_count == 1 else 0.70

    for i in potential_matches:
        if pattern_line_count <= 2:
            similarity = 1.0
        else:
            content_middle = "\n".join(norm_content_lines[i + 1 : i + pattern_line_count - 1])
            pattern_middle = "\n".join(pattern_lines[1:-1])
            similarity = SequenceMatcher(None, content_middle, pattern_middle).ratio()

        if similarity >= threshold:
            start_pos, end_pos = _calculate_line_positions(
                orig_content_lines, i, i + pattern_line_count, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _strategy_context_aware(content: str, pattern: str) -> list[tuple[int, int]]:
    """策略 9：逐行相似度（50% 阈值）匹配。

    参数:
        content: 源内容。
        pattern: 查找模式。

    返回:
        命中 ``(start, end)`` 位置列表（已映射回原内容）。

    异常:
        无。

    副作用:
        无。
    """

    pattern_lines = pattern.split("\n")
    content_lines = content.split("\n")

    if not pattern_lines:
        return []

    matches: list[tuple[int, int]] = []
    pattern_line_count = len(pattern_lines)

    for i in range(len(content_lines) - pattern_line_count + 1):
        block_lines = content_lines[i : i + pattern_line_count]

        high_similarity_count = 0
        for p_line, c_line in zip(pattern_lines, block_lines, strict=False):
            sim = SequenceMatcher(None, p_line.strip(), c_line.strip()).ratio()
            if sim >= 0.80:
                high_similarity_count += 1

        if high_similarity_count >= len(pattern_lines) * 0.5:
            start_pos, end_pos = _calculate_line_positions(
                content_lines, i, i + pattern_line_count, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


# =============================================================================
# 辅助函数
# =============================================================================


def _calculate_line_positions(
    content_lines: list[str],
    start_line: int,
    end_line: int,
    content_length: int,
) -> tuple[int, int]:
    """从行索引计算原内容中的起止字符位置。

    参数:
        content_lines: 按行拆分的内容（不含换行符）。
        start_line: 起始行索引（0 基，含）。
        end_line: 结束行索引（0 基，不含）。
        content_length: 原内容字符串总长度。

    返回:
        原内容中的 ``(start_pos, end_pos)``。

    异常:
        无。

    副作用:
        无。
    """

    start_pos = sum(len(line) + 1 for line in content_lines[:start_line])
    end_pos = sum(len(line) + 1 for line in content_lines[:end_line]) - 1
    end_pos = min(content_length, end_pos)
    return start_pos, end_pos


def _find_normalized_matches(
    content: str,
    content_lines: list[str],
    content_normalized_lines: list[str],
    pattern: str,
    pattern_normalized: str,
) -> list[tuple[int, int]]:
    """在归一化内容中查找匹配并映射回原内容位置。

    参数:
        content: 原内容字符串。
        content_lines: 原内容按行拆分。
        content_normalized_lines: 归一化后的按行拆分。
        pattern: 原始模式。
        pattern_normalized: 归一化后的模式。

    返回:
        原内容中的 ``(start, end)`` 命中位置列表。

    异常:
        无。

    副作用:
        无。
    """

    pattern_norm_lines = pattern_normalized.split("\n")
    num_pattern_lines = len(pattern_norm_lines)

    matches: list[tuple[int, int]] = []

    for i in range(len(content_normalized_lines) - num_pattern_lines + 1):
        block = "\n".join(content_normalized_lines[i : i + num_pattern_lines])

        if block == pattern_normalized:
            start_pos, end_pos = _calculate_line_positions(
                content_lines, i, i + num_pattern_lines, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _map_normalized_positions(
    original: str,
    normalized: str,
    normalized_matches: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """把归一化字符串中的位置映射回原字符串位置（空白归一化最佳努力）。

    参数:
        original: 原字符串。
        normalized: 归一化字符串。
        normalized_matches: 归一化空间的命中位置。

    返回:
        原字符串空间的命中位置列表。

    异常:
        无。

    副作用:
        无。
    """

    if not normalized_matches:
        return []

    orig_to_norm: list[int] = []

    orig_idx = 0
    norm_idx = 0

    while orig_idx < len(original) and norm_idx < len(normalized):
        if original[orig_idx] == normalized[norm_idx]:
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            norm_idx += 1
        elif original[orig_idx] in " \t" and normalized[norm_idx] == " ":
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            if orig_idx < len(original) and original[orig_idx] not in " \t":
                norm_idx += 1
        elif original[orig_idx] in " \t":
            orig_to_norm.append(norm_idx)
            orig_idx += 1
        else:
            orig_to_norm.append(norm_idx)
            orig_idx += 1

    while orig_idx < len(original):
        orig_to_norm.append(len(normalized))
        orig_idx += 1

    norm_to_orig_start: dict[int, int] = {}
    norm_to_orig_end: dict[int, int] = {}

    for orig_pos, norm_pos in enumerate(orig_to_norm):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos
        norm_to_orig_end[norm_pos] = orig_pos

    original_matches: list[tuple[int, int]] = []
    for norm_start, norm_end in normalized_matches:
        if norm_start in norm_to_orig_start:
            orig_start = norm_to_orig_start[norm_start]
        else:
            orig_start = min(i for i, n in enumerate(orig_to_norm) if n >= norm_start)

        if norm_end - 1 in norm_to_orig_end:
            orig_end = norm_to_orig_end[norm_end - 1] + 1
        else:
            orig_end = orig_start + (norm_end - norm_start)

        if norm_end < len(normalized) and normalized[norm_end - 1] == " ":
            while orig_end < len(original) and original[orig_end] in " \t":
                orig_end += 1

        original_matches.append((orig_start, min(orig_end, len(original))))

    return original_matches


def find_closest_lines(
    old_string: str,
    content: str,
    context_lines: int = 2,
    max_results: int = 3,
) -> str:
    """查找与 old_string 最相似的行，用于 "did you mean?" 反馈。

    参数:
        old_string: 原始查找文本。
        content: 源文件内容。
        context_lines: 上下文行数。
        max_results: 最多返回的结果数。

    返回:
        格式化的最近匹配片段；无可用时返回空字符串。

    异常:
        无。

    副作用:
        无。
    """

    if not old_string or not content:
        return ""

    old_lines = old_string.splitlines()
    content_lines = content.splitlines()

    if not old_lines or not content_lines:
        return ""

    anchor = old_lines[0].strip()
    if not anchor:
        candidates = [line.strip() for line in old_lines if line.strip()]
        if not candidates:
            return ""
        anchor = candidates[0]

    scored: list[tuple[float, int]] = []
    for i, line in enumerate(content_lines):
        stripped = line.strip()
        if not stripped:
            continue
        ratio = SequenceMatcher(None, anchor, stripped).ratio()
        if ratio > 0.3:
            scored.append((ratio, i))

    if not scored:
        return ""

    scored.sort(key=lambda x: -x[0])
    top = scored[:max_results]

    parts: list[str] = []
    seen_ranges: set[tuple[int, int]] = set()
    for _, line_idx in top:
        start = max(0, line_idx - context_lines)
        end = min(len(content_lines), line_idx + len(old_lines) + context_lines)
        key = (start, end)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        snippet = "\n".join(
            f"{start + j + 1:4d}| {content_lines[start + j]}" for j in range(end - start)
        )
        parts.append(snippet)

    if not parts:
        return ""

    return "\n---\n".join(parts)


def format_no_match_hint(
    error: str | None,
    match_count: int,
    old_string: str,
    content: str,
) -> str:
    """为纯无匹配错误返回 "Did you mean..." 片段。

    参数:
        error: 匹配错误文本。
        match_count: 命中数（仅当为 0 且错误以 "Could not find" 开头时触发）。
        old_string: 原始查找文本。
        content: 源文件内容。

    返回:
        拼接好的提示片段；无可提示时返回空字符串。

    异常:
        无。

    副作用:
        无。
    """

    if match_count != 0:
        return ""
    if not error or not error.startswith("Could not find"):
        return ""
    hint = find_closest_lines(old_string, content)
    if not hint:
        return ""
    return "\n\nDid you mean one of these sections?\n" + hint
