"""用户输入胶囊文本语法的解析、序列化与纯文本投影（前端同语法的唯一对照实现）。

胶囊是用户消息里的「指针」，模型直接读它去取正文，不把正文搬进上下文：

- ``<file path="/abs/notes.md" />``：本机文件附件（绝对路径；目录以 ``/`` 结尾）。
- ``<ref path="src/a.py" lines="12-14" />``：工作区引用（相对路径；PDF 用 ``page``）。
- ``<skill name="review-code" path="/abs/…/SKILL.md" />``：用户为本条消息选择的 Skill。

属性值按 XML 实体转义；标签只在代码块/行内代码之外生效（``find_message_tokens`` 负责跳过代码）。
语法不合法的标签按普通文本保留。跨语言用例见
``apps/desktop/lib/assistant/capsules/contract/capsule-grammar-cases.json``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Literal, TypeAlias

from app.utils.message_tokens import find_message_tokens


@dataclass(frozen=True, slots=True)
class FileCapsule:
    """本机文件附件；``path`` 是绝对路径，目录以 ``/`` 结尾。"""

    path: str
    kind: ClassVar[Literal["file"]] = "file"


@dataclass(frozen=True, slots=True)
class RefCapsule:
    """工作区引用；``lines`` 与 ``page`` 至多一个。"""

    path: str
    lines: str | None = None
    page: str | None = None
    kind: ClassVar[Literal["ref"]] = "ref"


@dataclass(frozen=True, slots=True)
class SkillCapsule:
    """用户为本条消息选择的 Skill；``path`` 由后端解析目录后写入。"""

    name: str
    path: str | None = None
    kind: ClassVar[Literal["skill"]] = "skill"


Capsule: TypeAlias = FileCapsule | RefCapsule | SkillCapsule


@dataclass(frozen=True, slots=True)
class TextSegment:
    """两个胶囊之间的普通文本。"""

    text: str


@dataclass(frozen=True, slots=True)
class CapsuleSegment:
    """一个已解析的胶囊。"""

    capsule: Capsule


CapsuleSegmentLike: TypeAlias = TextSegment | CapsuleSegment

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


def escape_attribute(value: str) -> str:
    """转义要放进 XML 属性的值；``&`` 最先替换，避免把后续实体二次转义。"""

    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def unescape_attribute(value: str) -> str:
    """还原 :func:`escape_attribute` 的转义；``&amp;`` 最后还原。"""

    return (
        value.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    )


def parse_capsules(text: str) -> list[CapsuleSegmentLike]:
    """把消息正文切成普通文本与胶囊；语法不合法/代码块里的标签按普通文本保留。"""

    segments: list[CapsuleSegmentLike] = []
    cursor = 0
    for match in find_message_tokens(CAPSULE_TAG_PATTERN, text):
        capsule = _capsule_from_match(match)
        if capsule is None:
            continue
        if match.start() > cursor:
            segments.append(TextSegment(text=text[cursor : match.start()]))
        segments.append(CapsuleSegment(capsule=capsule))
        cursor = match.end()
    if cursor < len(text):
        segments.append(TextSegment(text=text[cursor:]))
    return segments


def iter_capsules(text: str) -> list[Capsule]:
    """只取正文里的胶囊（含代码块外、语法合法的全部）。"""

    return [
        segment.capsule for segment in parse_capsules(text) if isinstance(segment, CapsuleSegment)
    ]


def serialize_capsule(capsule: Capsule) -> str:
    """序列化为规范形态（固定属性顺序、单空格、自闭合）；所有写入口只经这里产出标签。"""

    if isinstance(capsule, FileCapsule):
        return f'<file path="{escape_attribute(capsule.path)}" />'
    if isinstance(capsule, RefCapsule):
        locator = ""
        if capsule.lines is not None:
            locator = f' lines="{escape_attribute(capsule.lines)}"'
        elif capsule.page is not None:
            locator = f' page="{escape_attribute(capsule.page)}"'
        return f'<ref path="{escape_attribute(capsule.path)}"{locator} />'
    path = f' path="{escape_attribute(capsule.path)}"' if capsule.path is not None else ""
    return f'<skill name="{escape_attribute(capsule.name)}"{path} />'


def serialize_segments(segments: list[CapsuleSegmentLike]) -> str:
    """把解析结果按规范写法拼回正文（规范化用，不改变普通文本）。"""

    return "".join(
        segment.text if isinstance(segment, TextSegment) else serialize_capsule(segment.capsule)
        for segment in segments
    )


def plain_text(text: str) -> str:
    """展示/标题用的纯文本投影：胶囊换成可读名称，普通文本原样。"""

    return "".join(
        segment.text if isinstance(segment, TextSegment) else _capsule_plain_text(segment.capsule)
        for segment in parse_capsules(text)
    )


def _capsule_plain_text(capsule: Capsule) -> str:
    if isinstance(capsule, FileCapsule):
        return _basename(capsule.path)
    if isinstance(capsule, RefCapsule):
        if capsule.lines is not None:
            locator = f":{capsule.lines}"
        elif capsule.page is not None:
            locator = f" (p.{capsule.page})"
        else:
            locator = ""
        return f"{capsule.path or '工作区'}{locator}"
    return capsule.name


def _basename(path: str) -> str:
    trimmed = path.rstrip("/\\")
    return trimmed.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] or trimmed or path


def _capsule_from_match(match: re.Match[str]) -> Capsule | None:
    tag = match.group(1)
    required, optional = _ALLOWED_ATTRIBUTES[tag]
    attributes = {}
    for attribute in _ATTRIBUTE_PATTERN.finditer(match.group(2)):
        key = attribute.group(1)
        if key not in required and key not in optional:
            return None
        if key in attributes:
            return None
        attributes[key] = unescape_attribute(attribute.group(2))
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
        if lines is not None and not _valid_lines(lines):
            return None
        if page is not None and not _PAGE_PATTERN.fullmatch(page):
            return None
        return RefCapsule(path=attributes["path"], lines=lines, page=page)
    name = attributes["name"]
    path = attributes.get("path")
    if not name or path == "":
        return None
    return SkillCapsule(name=name, path=path)


def _valid_lines(lines: str) -> bool:
    match = _LINES_PATTERN.fullmatch(lines)
    if match is None:
        return False
    return match.group(2) is None or int(match.group(1)) <= int(match.group(2))


__all__ = [
    "CAPSULE_TAG_PATTERN",
    "Capsule",
    "CapsuleSegment",
    "CapsuleSegmentLike",
    "FileCapsule",
    "RefCapsule",
    "SkillCapsule",
    "TextSegment",
    "escape_attribute",
    "iter_capsules",
    "parse_capsules",
    "plain_text",
    "serialize_capsule",
    "serialize_segments",
    "unescape_attribute",
]
