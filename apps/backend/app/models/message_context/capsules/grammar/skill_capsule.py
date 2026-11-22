"""用户为本条消息选择的 Skill 胶囊。"""

from dataclasses import dataclass
from typing import ClassVar, Literal

from app.models.message_context.capsules.grammar.capsule import Capsule


@dataclass(frozen=True, slots=True)
class SkillCapsule(Capsule):
    """用户为本条消息选择的 Skill；``path`` 由后端解析目录后写入。"""

    name: str
    path: str | None = None
    kind: ClassVar[Literal["skill"]] = "skill"
