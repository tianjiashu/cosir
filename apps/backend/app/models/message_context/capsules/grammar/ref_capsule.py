"""工作区引用的胶囊。"""

from dataclasses import dataclass
from typing import ClassVar, Literal

from app.models.message_context.capsules.grammar.capsule import Capsule


@dataclass(frozen=True, slots=True)
class RefCapsule(Capsule):
    """工作区引用；``lines`` 与 ``page`` 至多一个。"""

    path: str
    lines: str | None = None
    page: str | None = None
    kind: ClassVar[Literal["ref"]] = "ref"
