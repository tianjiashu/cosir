"""用户输入胶囊文本语法的解析与序列化（前端同语法的唯一对照实现）。

胶囊是用户消息里的「指针」，模型直接读它去取正文，不把正文搬进上下文：

- ``<file path="/abs/notes.md" />``：本机文件附件（绝对路径；目录以 ``/`` 结尾）。
- ``<ref path="src/a.py" lines="12-14" />``：工作区引用（相对路径；PDF 用 ``page``）。
- ``<skill name="review-code" path="/abs/…/SKILL.md" />``：用户为本条消息选择的 Skill。

属性值按 XML 实体转义；标签只在代码块/行内代码之外生效（``Capsule.find_outside_code`` 负责跳过代码）。
语法不合法的标签按普通文本保留。跨语言用例见
``apps/desktop/lib/assistant/capsules/contract/capsule-grammar-cases.json``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.message_context.capsules.grammar.capsule import Capsule
from app.models.message_context.capsules.grammar.file_capsule import FileCapsule
from app.models.message_context.capsules.grammar.ref_capsule import RefCapsule
from app.models.message_context.capsules.grammar.skill_capsule import SkillCapsule

# 与前端 ``CAPSULE_TAG_PATTERN`` 完全一致；属性值里不会出现裸双引号（``"`` 已转义）。
CAPSULE_TAG_PATTERN = re.compile(r'<(file|ref|skill)\s+([a-z]+="[^"]*"(?:\s+[a-z]+="[^"]*")*)\s*/>')
_ATTRIBUTE_PATTERN = re.compile(r'([a-z]+)="([^"]*)"')
_LINES_PATTERN = re.compile(r"^(\d+)(?:-(\d+))?$")
_PAGE_PATTERN = re.compile(r"^[1-9]\d*$")

_ALLOWED_ATTRIBUTES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "file": (frozenset({"path"}), frozenset()),
    "ref": (frozenset({"path"}), frozenset({"lines", "page"})),
    "skill": (frozenset({"name"}), frozenset({"path"})),
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

    @classmethod
    def plain_text(cls, text: str) -> str:
        """展示用纯文本投影：胶囊换成可读名称，普通文本原样。"""

        parts: list[str] = []
        for segment in cls.parse(text):
            if isinstance(segment, TextSegment):
                parts.append(segment.text)
            elif isinstance(segment, CapsuleSegment):
                parts.append(cls._display_name(segment.capsule))
            else:
                raise TypeError(f"unsupported segment: {type(segment).__name__}")
        return "".join(parts)

    @staticmethod
    def _display_name(capsule: Capsule) -> str:
        if isinstance(capsule, FileCapsule):
            return Segment._basename(capsule.path)
        if isinstance(capsule, RefCapsule):
            if capsule.lines is not None:
                locator = f":{capsule.lines}"
            elif capsule.page is not None:
                locator = f" (p.{capsule.page})"
            else:
                locator = ""
            return f"{capsule.path or '工作区'}{locator}"
        if isinstance(capsule, SkillCapsule):
            return capsule.name
        raise TypeError(f"unsupported capsule: {type(capsule).__name__}")

    @staticmethod
    def _basename(path: str) -> str:
        trimmed = path.rstrip("/\\")
        return trimmed.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or trimmed or path

    @staticmethod
    def unescape_attribute(value: str) -> str:
        """还原标签属性的 XML 转义；``&amp;`` 最后还原（与 ``Capsule._escape_attribute`` 对应）。"""

        return (
            value.replace("&quot;", '"')
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&amp;", "&")
        )

    @classmethod
    def _capsule_from_match(cls, match: re.Match[str]) -> Capsule | None:
        """把一次标签匹配解析成胶囊；属性不在白名单、缺必需项或取值非法时返回空值。"""

        tag = match.group(1)
        required, optional = _ALLOWED_ATTRIBUTES[tag]
        attributes = {}
        for attribute in _ATTRIBUTE_PATTERN.finditer(match.group(2)):
            key = attribute.group(1)
            if key not in required and key not in optional:
                return None
            if key in attributes:
                return None
            attributes[key] = cls.unescape_attribute(attribute.group(2))
        if any(key not in attributes for key in required):
            return None

        if tag == "file":
            path = attributes["path"]
            return FileCapsule(path=path) if path else None
        if tag == "ref":
            lines = attributes.get("lines")
            page = attributes.get("page")
            if lines is not None and page is not None:
                return None
            if lines is not None and not cls._valid_lines(lines):
                return None
            if page is not None and not _PAGE_PATTERN.fullmatch(page):
                return None
            return RefCapsule(path=attributes["path"], lines=lines, page=page)
        name = attributes["name"]
        skill_path = attributes.get("path")
        if not name or skill_path == "":
            return None
        return SkillCapsule(name=name, path=skill_path)

    @staticmethod
    def _valid_lines(lines: str) -> bool:
        match = _LINES_PATTERN.fullmatch(lines)
        if match is None:
            return False
        return match.group(2) is None or int(match.group(1)) <= int(match.group(2))


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
