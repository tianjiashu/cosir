"""用户输入胶囊文本语法的解析与序列化（前端同语法的唯一对照实现）。

前三种胶囊是用户消息里的「指针」，模型直接读它去取正文，不把正文搬进上下文；摘录自带原文：

- ``<file path="/abs/notes.md" />``：本机文件附件（绝对路径；目录以 ``/`` 结尾）。
- ``<ref path="src/a.py" lines="12-14" />``：工作区引用（相对路径；PDF 用 ``page``）。
- ``<skill name="review-code" path="/abs/…/SKILL.md" />``：用户为本条消息选择的 Skill。
- ``<quote path="src/a.py" lines="3-4" text="…" note="…" />``：选中的原文与批注（见 ``QuoteCapsule``）。

属性值按 XML 实体转义（换行、反引号等也转成数字实体，见 ``Capsule.escape_attribute``）；
标签只在代码块/行内代码之外生效（``Capsule.find_outside_code`` 负责跳过代码）。
语法不合法的标签按普通文本保留。跨语言用例见
``apps/desktop/lib/assistant/capsules/contract/capsule-grammar-cases.json``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.message_context.capsules.grammar.capsule import Capsule
from app.models.message_context.capsules.grammar.file_capsule import FileCapsule
from app.models.message_context.capsules.grammar.quote_capsule import QuoteCapsule
from app.models.message_context.capsules.grammar.ref_capsule import RefCapsule
from app.models.message_context.capsules.grammar.skill_capsule import SkillCapsule

# 与前端 ``CAPSULE_TAG_PATTERN`` 完全一致；属性值里不会出现裸双引号（``"`` 已转义）。
CAPSULE_TAG_PATTERN = re.compile(r'<(file|ref|skill|quote)\s+([a-z]+="[^"]*"(?:\s+[a-z]+="[^"]*")*)\s*/>')
_ATTRIBUTE_PATTERN = re.compile(r'([a-z]+)="([^"]*)"')

# 标签名 → 胶囊类型；与上面正则的分支一一对应。
_CAPSULE_TYPES: dict[str, type[Capsule]] = {
    capsule_type.kind: capsule_type
    for capsule_type in (FileCapsule, RefCapsule, SkillCapsule, QuoteCapsule)
}


@dataclass(frozen=True, slots=True)
class Segment:
    """解析结果的一段：普通文本或一个已解析的胶囊；文本 ↔ 段落的入口都在本类上。"""

    def serialize(self) -> str:
        """按规范写法拼回本段；所有写入口只经这里产出标签。"""

        raise NotImplementedError

    @classmethod
    def parse(cls, text: str) -> list[Segment]:
        """把消息正文切成普通文本与胶囊；语法不合法/代码块里的标签按普通文本保留。"""

        segments: list[Segment] = []
        cursor = 0
        for match in Capsule.find_outside_code(CAPSULE_TAG_PATTERN, text):
            capsule = cls._capsule_from_match(match)
            if capsule is None:
                continue
            if match.start() > cursor:
                segments.append(TextSegment(text=text[cursor : match.start()]))
            segments.append(CapsuleSegment(capsule=capsule))
            cursor = match.end()
        if cursor < len(text):
            segments.append(TextSegment(text=text[cursor:]))
        return segments

    @staticmethod
    def serialize_all(segments: list[Segment]) -> str:
        """把解析结果按规范写法拼回正文（规范化用，不改变普通文本）。"""

        return "".join(segment.serialize() for segment in segments)

    @staticmethod
    def _capsule_from_match(match: re.Match[str]) -> Capsule | None:
        """把一次标签匹配解析成胶囊；属性不在白名单、重复、缺必需项或取值非法时返回空值。"""

        capsule_type = _CAPSULE_TYPES[match.group(1)]
        allowed = capsule_type.REQUIRED | capsule_type.OPTIONAL
        attributes: dict[str, str] = {}
        for attribute in _ATTRIBUTE_PATTERN.finditer(match.group(2)):
            key = attribute.group(1)
            if key not in allowed or key in attributes:
                return None
            attributes[key] = Capsule.unescape_attribute(attribute.group(2))
        if not attributes.keys() >= capsule_type.REQUIRED:
            return None
        return capsule_type.from_attributes(attributes)


@dataclass(frozen=True, slots=True)
class TextSegment(Segment):
    """两个胶囊之间的普通文本。"""

    text: str

    def serialize(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class CapsuleSegment(Segment):
    """一个已解析的胶囊。"""

    capsule: Capsule

    def serialize(self) -> str:
        return self.capsule.serialize()


__all__ = [
    "CAPSULE_TAG_PATTERN",
    "CapsuleSegment",
    "Segment",
    "TextSegment",
]
