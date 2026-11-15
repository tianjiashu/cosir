"""导入用户通过桌面原生选择器选中的 Skill 来源。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SkillsImportRequest(BaseModel):
    """描述本机 Skill 文件夹或 ZIP 的来源路径。

    ``resolution`` 只在上一次导入返回 409 ``skill_import_conflict`` 后、用户作出选择时带上：``replace`` 替换
    专属目录里的同名 Skill，``override`` 导入并在本页覆盖别处的同名 Skill。后端会重新判定，不符就再次 409。
    """

    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(min_length=1)
    source_type: Literal["directory", "archive"]
    resolution: Literal["replace", "override"] | None = None
