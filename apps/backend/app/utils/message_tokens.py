"""普通消息中的附件标记只在代码外生效；此模块不识别工作区引用或持久化任何状态。"""

import re
from collections.abc import Callable, Iterator
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline, backtick
from markdown_it.token import Token


def _record_backticks(state: StateInline, silent: bool) -> bool:
    start, count = state.pos, len(state.tokens)
    matched = backtick(state, silent)
    if (
        matched
        and not silent
        and len(state.tokens) > count
        and state.tokens[-1].type == "code_inline"
    ):
        state.env["code_ranges"].append((start, state.pos))
    return matched


_parser = MarkdownIt("commonmark")
_parser.inline.ruler.at("backticks", _record_backticks)


def _text_regions(text: str) -> Iterator[tuple[int, int]]:
    """成熟解析器判定围栏和代码跨度；偏移始终指向原文，替换不重新排版 Markdown。"""
    offsets = [0]
    for ending in re.finditer(r"\r\n|\r|\n", text):
        offsets.append(ending.end())
    if offsets[-1] < len(text):
        offsets.append(len(text))
    blocks = [
        (offsets[token.map[0]], offsets[token.map[1]])
        for token in _parser.parse(text, {"code_ranges": []})
        if token.type in {"fence", "code_block"} and token.map is not None
    ]
    cursor = 0
    for start, end in [*blocks, (len(text), len(text))]:
        segment = text[cursor:start]
        env: dict[str, Any] = {"code_ranges": []}
        tokens: list[Token] = []
        _parser.inline.parse(segment, _parser, env, tokens)
        local = 0
        for left, right in sorted(env["code_ranges"]):
            if local < left:
                yield cursor + local, cursor + left
            local = max(local, right)
        if local < len(segment):
            yield cursor + local, start
        cursor = end


def find_message_tokens(pattern: re.Pattern[str], text: str) -> Iterator[re.Match[str]]:
    if not pattern.search(text):
        return
    for start, end in _text_regions(text):
        yield from pattern.finditer(text, start, end)


def replace_message_tokens(
    pattern: re.Pattern[str], replacement: str | Callable[[re.Match[str]], str], text: str
) -> str:
    pieces: list[str] = []
    cursor = 0
    for match in find_message_tokens(pattern, text):
        pieces.append(text[cursor : match.start()])
        pieces.append(replacement(match) if callable(replacement) else match.expand(replacement))
        cursor = match.end()
    return "".join([*pieces, text[cursor:]])
