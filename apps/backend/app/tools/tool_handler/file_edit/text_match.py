"""原文匹配：``edit_file`` 与 ``apply_patch`` 定位要改的文本，失败时给出最接近的几行。

只认与文件原文完全一致的片段，且默认必须唯一命中。曾经用过一条逐级放宽的模糊匹配链（空白、缩进、
转义、Unicode、首尾行锚定），它在第一个「找到了」的级别就停，靠后的级别按相似度定位，会把改动落到
另一处相似的代码上、仍报成功，模型也因此学不到自己的 ``old_string`` 写错了。精确失败的代价只是
重读一次文件再改，所以不再容忍差异；最近行提示保留，它是重试时最有用的信息。纯逻辑，不读写文件。
"""

from difflib import SequenceMatcher


class TextMatchError(ValueError):
    """原文没有命中或命中多处；消息是面向模型的英文短句，没命中时附最近行提示。"""


def replace_exact(content: str, old: str, new: str, *, replace_all: bool = False) -> tuple[str, int]:
    """把 ``content`` 里的 ``old`` 原样替换为 ``new``，返回新内容与替换处数。

    ``replace_all`` 为 False 时要求 ``old`` 恰好出现一次。``old`` 为空由调用方保证不会出现。

    异常:
        TextMatchError: 没有命中，或不允许多处时命中多处。
    """

    count = content.count(old)
    if count == 0:
        hint = find_closest_lines(old, content)
        raise TextMatchError(
            "no exact match for the text in the file"
            + (f"\n\nDid you mean one of these sections?\n{hint}" if hint else "")
        )
    if count > 1 and not replace_all:
        raise TextMatchError(
            f"the text matches {count} places in the file; include more surrounding lines "
            "to make it unique, or replace every occurrence"
        )
    return content.replace(old, new), count


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
