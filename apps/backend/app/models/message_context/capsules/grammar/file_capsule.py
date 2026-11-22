"""本机文件附件的胶囊。"""

from dataclasses import dataclass
from typing import ClassVar, Literal

from app.models.message_context.capsules.grammar.capsule import Capsule


@dataclass(frozen=True, slots=True)
class FileCapsule(Capsule):
    """本机文件附件；``path`` 是绝对路径，目录以 ``/`` 结尾。"""

    path: str
    kind: ClassVar[Literal["file"]] = "file"
