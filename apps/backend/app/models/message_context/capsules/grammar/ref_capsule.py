"""工作区引用的胶囊。"""

from dataclasses import dataclass
from typing import ClassVar, Literal, Self

from app.models.message_context.capsules.grammar.capsule import Capsule


@dataclass(frozen=True, slots=True)
class RefCapsule(Capsule):
    """工作区引用；``lines`` 与 ``page`` 至多一个。"""

    path: str
    lines: str | None = None
    page: str | None = None
    kind: ClassVar[Literal["ref"]] = "ref"
    REQUIRED: ClassVar[frozenset[str]] = frozenset({"path"})
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"lines", "page"})

    @classmethod
    def from_attributes(cls, attributes: dict[str, str]) -> Self | None:
        locator = cls._locator(attributes)
        return None if locator is None else cls(path=attributes["path"], **locator)

    def serialize(self) -> str:
        # lines 与 page 互斥；都写进 _tag，空值的那个不会出现。
        return self._tag(("path", self.path), ("lines", self.lines), ("page", self.page))
