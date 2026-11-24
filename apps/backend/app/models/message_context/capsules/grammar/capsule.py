"""用户消息里的胶囊：基类负责代码区判定、属性转义与规范写法，各形状在同目录子类里声明自己的属性。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline, backtick
from markdown_it.token import Token

# 属性值的转义表（与前端 ``ATTRIBUTE_ESCAPES`` 同序）。除 XML 的四个实体外，换行、制表、反引号与 ``~``
# 也转成数字实体：胶囊外的 Markdown 预扫描（``find_outside_code``）看不懂标签，属性值里的这些字符会和
# 标签外的文本配对成围栏、缩进代码或行内代码，把标签切断。转义后任何属性值都是单行、不含代码标记，
# 摘录这样携带整段 Markdown 的胶囊才能安全放进正文。``&`` 必须最先转义、最后还原。
_ATTRIBUTE_ESCAPES: tuple[tuple[str, str], ...] = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("\n", "&#10;"),
    ("\r", "&#13;"),
    ("\t", "&#9;"),
    ("`", "&#96;"),
    ("~", "&#126;"),
)


@dataclass(frozen=True, slots=True)
class Capsule:
    """胶囊：文本 ↔ 段落的解析见 ``capsule_grammar``，子类声明标签名、属性白名单、取值校验与写法。

    基类不给 ``kind`` 默认值：每个子类声明自己的字面量，读取方按 ``isinstance`` 或 ``kind`` 区分。
    """

    kind: ClassVar[str]
    REQUIRED: ClassVar[frozenset[str]]
    OPTIONAL: ClassVar[frozenset[str]] = frozenset()
    _LINES: ClassVar[re.Pattern[str]] = re.compile(r"^(\d+)(?:-(\d+))?$")
    _PAGE: ClassVar[re.Pattern[str]] = re.compile(r"^[1-9]\d*$")

    # 成熟 Markdown 解析器判定围栏与行内代码；只认 commonmark，不引入额外排版语义。不识别 HTML 块（与前端
    # ``markdownSegments`` 一致）：独占一行的胶囊标签会被当成 HTML 块，紧跟着的围栏就不再是代码。
    _parser: ClassVar[MarkdownIt] = MarkdownIt("commonmark").disable("html_block")

    @staticmethod
    def _record_backticks(state: StateInline, silent: bool) -> bool:
        """backticks 规则替身：先调原规则，再把行内代码区间记进 ``env["code_ranges"]``。"""

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

    @staticmethod
    def _text_regions(text: str) -> Iterator[tuple[int, int]]:
        """成熟解析器判定围栏和代码跨度；偏移始终指向原文，替换不重新排版 Markdown。"""

        offsets = [0]
        for ending in re.finditer(r"\r\n|\r|\n", text):
            offsets.append(ending.end())
        if offsets[-1] < len(text):
            offsets.append(len(text))
        blocks = [
            (offsets[token.map[0]], offsets[token.map[1]])
            for token in Capsule._parser.parse(text, {"code_ranges": []})
            if token.type in {"fence", "code_block"} and token.map is not None
        ]
        cursor = 0
        for start, end in [*blocks, (len(text), len(text))]:
            segment = text[cursor:start]
            env: dict[str, Any] = {"code_ranges": []}
            tokens: list[Token] = []
            Capsule._parser.inline.parse(segment, Capsule._parser, env, tokens)
            local = 0
            for left, right in sorted(env["code_ranges"]):
                if local < left:
                    yield cursor + local, cursor + left
                local = max(local, right)
            if local < len(segment):
                yield cursor + local, start
            cursor = end

    @classmethod
    def find_outside_code(cls, pattern: re.Pattern[str], text: str) -> Iterator[re.Match[str]]:
        """找出代码块与行内代码之外的匹配；偏移指向原文。"""

        if not pattern.search(text):
            return
        for start, end in cls._text_regions(text):
            yield from pattern.finditer(text, start, end)

    @classmethod
    def iter_capsules(cls, text: str) -> list[Capsule]:
        """只取正文里的胶囊（含代码块外、语法合法的全部）。"""

        from app.models.message_context.capsules.capsule_grammar import (
            CapsuleSegment,
            Segment,
        )

        return [
            segment.capsule
            for segment in Segment.parse(text)
            if isinstance(segment, CapsuleSegment)
        ]

    @classmethod
    def from_attributes(cls, attributes: dict[str, str]) -> Self | None:
        """由已还原转义、且已通过白名单的属性构造胶囊；取值不合法时返回空值（标签按普通文本保留）。"""

        raise NotImplementedError

    def serialize(self) -> str:
        """序列化为规范形态（固定属性顺序、单空格、自闭合）；所有写入口只经这里产出标签。"""

        raise NotImplementedError

    @classmethod
    def _locator(cls, attributes: dict[str, str]) -> dict[str, str] | None:
        """工作区位置（``lines`` 行范围或 PDF ``page`` 页码，至多一个）；不合法时返回空值。``ref`` 与来自文件的
        ``quote`` 共用这一条规则。"""

        lines = attributes.get("lines")
        page = attributes.get("page")
        if lines is not None and page is not None:
            return None
        if lines is not None:
            match = cls._LINES.fullmatch(lines)
            if match is None or (match.group(2) is not None and int(match.group(1)) > int(match.group(2))):
                return None
            return {"lines": lines}
        if page is not None:
            return {"page": page} if cls._PAGE.fullmatch(page) else None
        return {}

    def _tag(self, *attributes: tuple[str, str | None]) -> str:
        """按给定顺序写出标签；值为 ``None`` 的属性不写。"""

        written = "".join(
            f' {name}="{self.escape_attribute(value)}"' for name, value in attributes if value is not None
        )
        return f"<{self.kind}{written} />"

    @staticmethod
    def escape_attribute(value: str) -> str:
        """按 ``_ATTRIBUTE_ESCAPES`` 转义属性值。"""

        for raw, entity in _ATTRIBUTE_ESCAPES:
            value = value.replace(raw, entity)
        return value

    @staticmethod
    def unescape_attribute(value: str) -> str:
        """还原 ``escape_attribute``：逆序替换，``&amp;`` 最后还原，避免把 ``&amp;lt;`` 解成 ``<``。"""

        for raw, entity in reversed(_ATTRIBUTE_ESCAPES):
            value = value.replace(entity, raw)
        return value


# 规则替换要在类存在之后做：类体里拿到的是 staticmethod 对象，这里取到的是原函数。
Capsule._parser.inline.ruler.at("backticks", Capsule._record_backticks)
