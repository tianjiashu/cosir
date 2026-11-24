"""用户为本条消息选择的 Skill 胶囊。"""

from dataclasses import dataclass
from typing import ClassVar, Literal, Self

from app.models.message_context.capsules.grammar.capsule import Capsule


@dataclass(frozen=True, slots=True)
class SkillCapsule(Capsule):
    """用户为本条消息选择的 Skill；``path`` 由后端解析目录后写入。"""

    name: str
    path: str | None = None
    kind: ClassVar[Literal["skill"]] = "skill"
    REQUIRED: ClassVar[frozenset[str]] = frozenset({"name"})
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"path"})

    @classmethod
    def from_attributes(cls, attributes: dict[str, str]) -> Self | None:
        path = attributes.get("path")
        if not attributes["name"] or path == "":
            return None
        return cls(name=attributes["name"], path=path)

    def serialize(self) -> str:
        return self._tag(("name", self.name), ("path", self.path))
