"""模型读到过的文件版本与覆盖范围。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.tools.file_system.facts.file_version import FileVersion


class FileRead(BaseModel):
    """模型看到的文件版本及覆盖范围，行区间只记录完整行。"""

    model_config = ConfigDict(frozen=True)
    path: str
    version: FileVersion
    whole: bool = False
    lines: tuple[tuple[int, int], ...] = ()
    total_lines: int | None = None
