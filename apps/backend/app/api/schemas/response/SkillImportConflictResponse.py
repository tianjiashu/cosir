"""Skill 导入冲突明细：随 409 ``skill_import_conflict`` 返回，界面据此弹窗让用户决定。"""

from typing import Literal

from pydantic import BaseModel

from app.service.configuration.skills.skill_import import SkillImportConflict
from app.utils.path.shared_skills import home_relative


class SkillImportExistingResponse(BaseModel):
    """一条现有的同名 Skill；``same_content`` 表示与导入的内容相同（同指纹）。"""

    scope: Literal["system", "workspace"]
    kind: Literal["cosir", "shared"]
    location: str
    description: str
    same_content: bool


class SkillImportConflictResponse(BaseModel):
    """``relation``：``identical`` 已有内容相同的生效条目（不导入）；``replace`` 可替换专属目录里的那份；
    ``override`` 可导入并在本页覆盖别处那份。再次提交时带上 ``resolution``（与 ``relation`` 相同）。
    ``existing`` 按优先级从高到低。"""

    name: str
    relation: Literal["identical", "replace", "override"]
    description: str
    existing: list[SkillImportExistingResponse]

    @staticmethod
    def from_conflict(conflict: SkillImportConflict) -> "SkillImportConflictResponse":
        return SkillImportConflictResponse(
            name=conflict.incoming.name,
            relation=conflict.relation,
            description=conflict.incoming.description,
            existing=[
                SkillImportExistingResponse(
                    scope=info.scope.kind,
                    kind=info.root.kind,
                    location=home_relative(info.directory),
                    description=info.description,
                    same_content=info.fingerprint == conflict.incoming.fingerprint,
                )
                for info in conflict.existing
            ],
        )
