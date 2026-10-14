"""导入用户通过桌面原生选择器选中的 Skill 来源。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillsImportRequest(BaseModel):
    """描述本机 Skill 文件夹或 ZIP 的来源路径。"""

    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(min_length=1)
    source_type: Literal["directory", "archive"]
