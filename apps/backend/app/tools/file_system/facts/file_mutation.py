"""文件工具确认的读写前后版本。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.tools.file_system.facts.file_version import FileVersion


class FileMutation(BaseModel):
    """文件工具确认的读写前后版本，whole 表示完整新内容由模型提供。"""

    model_config = ConfigDict(frozen=True)
    path: str
    operation: Literal["create", "modify"]
    before: FileVersion | None
    after: FileVersion | None
    whole: bool = False
