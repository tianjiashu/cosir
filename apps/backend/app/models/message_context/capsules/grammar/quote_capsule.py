"""摘录胶囊：用户选中的一段原文，连同对它的批注与（来自工作区文件时的）位置。"""

from dataclasses import dataclass
from typing import ClassVar, Literal, Self

from app.models.message_context.capsules.grammar.capsule import Capsule


@dataclass(frozen=True, slots=True)
class QuoteCapsule(Capsule):
    """摘录：自带原文的胶囊。

    对话回复与工作区文件的选区都产出它。原文 ``text`` 随胶囊走：回复读不回来（回合可能已被撤回、来自子 Agent
    等别的会话、或已不在上下文窗口里），文件之后可能被改；来自工作区文件时另带 ``path`` 与位置（``lines`` 或
    ``page``，规则同 ``ref``），模型可按位置读上下文，路径在用户输入边界按 ``ref`` 同样校验。``note`` 是用户
    对这段的批注：原文与批注是一个整体，放在同一个胶囊里，不靠正文里的先后位置配对。原文上限
    ``MAX_TEXT_LENGTH`` 与前端截断用同一个值（见跨语言用例的 ``limits``），超限在用户输入边界拒绝。
    """

    text: str
    note: str | None = None
    path: str | None = None
    lines: str | None = None
    page: str | None = None
    kind: ClassVar[Literal["quote"]] = "quote"
    REQUIRED: ClassVar[frozenset[str]] = frozenset({"text"})
    OPTIONAL: ClassVar[frozenset[str]] = frozenset({"note", "path", "lines", "page"})
    MAX_TEXT_LENGTH: ClassVar[int] = 8000

    @classmethod
    def from_attributes(cls, attributes: dict[str, str]) -> Self | None:
        locator = cls._locator(attributes)
        path = attributes.get("path")
        # 位置只对文件有意义：有行号或页码就得有路径；来自对话回复的摘录没有路径。
        if locator is None or not attributes["text"].strip() or path == "" or (path is None and locator):
            return None
        return cls(text=attributes["text"], note=attributes.get("note") or None, path=path, **locator)

    def serialize(self) -> str:
        return self._tag(
            ("path", self.path), ("lines", self.lines), ("page", self.page), ("text", self.text), ("note", self.note)
        )

    def source(self) -> str | None:
        """给模型看的来源：``src/a.py:12-14`` / ``docs/p.pdf p.3``；来自对话回复时为空。"""

        if self.path is None:
            return None
        if self.lines is not None:
            return f"{self.path}:{self.lines}"
        return f"{self.path} p.{self.page}" if self.page is not None else self.path
