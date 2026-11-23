"""读过的文件在磁盘上发生变化时告知模型的事实。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.tools.file_system.facts.file_version import FileVersion


class StaleRead(BaseModel):
    """模型读过、之后在磁盘上变了的文件；``version`` 是告知时的磁盘版本，删除或移走为空。"""

    model_config = ConfigDict(frozen=True)
    path: str
    version: FileVersion | None
