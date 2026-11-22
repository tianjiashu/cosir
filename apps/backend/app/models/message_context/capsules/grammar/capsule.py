"""用户消息里的胶囊指针：基类与三种形状，连同它们自身的代码区判定、枚举与序列化。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline, backtick
from markdown_it.token import Token

if TYPE_CHECKING:
    from app.models.message_context.capsules.grammar.file_capsule import FileCapsule
    from app.models.message_context.capsules.grammar.ref_capsule import RefCapsule
    from app.models.message_context.capsules.grammar.skill_capsule import SkillCapsule


@dataclass(frozen=True, slots=True)
class Capsule:
    """胶囊指针：文本 ↔ 段落的解析与纯文本投影见 ``capsule_grammar``，子类只加各自的属性。

    基类不给 ``kind`` 默认值：每个子类声明自己的字面量，读取方按 ``isinstance`` 或 ``kind`` 区分。
    """

    kind: ClassVar[str]

    # 成熟 Markdown 解析器判定围栏与行内代码；只认 commonmark，不引入额外排版语义。
    _parser: ClassVar[MarkdownIt] = MarkdownIt("commonmark")

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

    @staticmethod
    def _escape_attribute(value: str) -> str:
        """转义要放进 XML 属性的值；``&`` 最先替换，避免把后续实体二次转义。"""

        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def serialize(self) -> str:
        """序列化为规范形态（固定属性顺序、单空格、自闭合）；所有写入口只经这里产出标签。"""

        if self.kind == "file":
            return f'<file path="{self._escape_attribute(cast("FileCapsule", self).path)}" />'
        if self.kind == "ref":
            capsule = cast("RefCapsule", self)
            locator = ""
            if capsule.lines is not None:
                locator = f' lines="{self._escape_attribute(capsule.lines)}"'
            elif capsule.page is not None:
                locator = f' page="{self._escape_attribute(capsule.page)}"'
            return f'<ref path="{self._escape_attribute(capsule.path)}"{locator} />'
        if self.kind == "skill":
            skill = cast("SkillCapsule", self)
            path = f' path="{self._escape_attribute(skill.path)}"' if skill.path is not None else ""
            return f'<skill name="{self._escape_attribute(skill.name)}"{path} />'
        raise TypeError(f"unsupported capsule: {type(self).__name__}")


# 规则替换要在类存在之后做：类体里拿到的是 staticmethod 对象，这里取到的是原函数。
Capsule._parser.inline.ruler.at("backticks", Capsule._record_backticks)
