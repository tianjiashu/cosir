"""一个作用域（系统级或 workspace 级）内 Agent Skill 的读取与启停用例；导入见 ``skills/skill_import``。

写操作只作用于本作用域自己的停用名单：workspace 配置页能看到继承来的系统级 Skill，但不能启停它们。

不提供删除：共享目录里多是别的工具装的，cosir 删了它们也一起失去；工作区的目录在 git 里，删除等于改仓库。
启停只改 cosir 自己的停用名单、可逆，真要删由用户在文件管理器里做（详情页有「在文件夹中显示」）。
"""

from __future__ import annotations

import builtins
from pathlib import Path

from app.config.logging.logger import log
from app.service.configuration import file_store
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.skills.catalog import (
    SkillCatalog,
    SkillInfo,
    read_skill_file,
)
from app.service.configuration.skills.configuration import SkillConfiguration
from app.service.configuration.skills.configuration_view import SkillCatalogView, SkillDetail


class SkillConfigurationService:
    """读取一个作用域看得到的 Skill、管理它的停用名单；``workspace_root`` 为空即系统级。"""

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self.scope = ExtensionScope(workspace_root)

    @property
    def catalog(self) -> SkillCatalog:
        """本作用域配置页看得到的目录：系统级只有系统层，workspace 级是两层合并。"""

        return SkillCatalog.visible_from(self.scope.workspace_root)

    def list(self) -> list[SkillInfo]:
        """列出可见的生效 Skill（输入框选择器用）；配置页的分组视图见 ``view``。"""

        return self.catalog.list_skills()

    def view(self) -> SkillCatalogView:
        """本作用域配置页的分组视图与 Agent 目录体量。"""

        return SkillCatalogView.build(self.scope.workspace_root)

    def read(self, name: str) -> SkillDetail:
        """读取指定 Skill 的详情（正文、元数据、文件清单）；配置界面查看不受启用状态限制。"""

        return SkillDetail.read(self.catalog, name)

    def preview_file(self, name: str, path: str) -> str:
        """读取指定 Skill 目录内一个文本文件给配置页预览，不受启用状态限制。"""

        return read_skill_file(self.catalog.find(name), path)

    def set_enabled(self, names: builtins.list[str], enabled: bool) -> None:
        """一次启用或停用本作用域的若干个 Skill，只写一次配置。

        全部名称都要是本作用域这一层的生效 Skill，任一不是就整体拒绝、什么都不写。

        异常:
            KeyError: 有名称不对应可见 Skill。
            ValueError: 有 Skill 属于另一个作用域（例如在 workspace 页启停系统级 Skill）。
        """

        with file_store.locked(self.scope.skills_config_file):
            visible = {info.name: info for info in self.catalog.list_skills()}
            for name in names:
                self._require_own(visible[name])
            configuration = SkillConfiguration.read(self.scope)
            targets = set(names)
            disabled = [item for item in configuration.disabled_skills if item not in targets]
            if not enabled:
                disabled.extend(sorted(targets))
            configuration.model_copy(update={"disabled_skills": disabled}).write(self.scope)
        log.info(
            "configuration_skill_enabled_written",
            extra={
                "msg": "Skill 启用状态已保存",
                "data": {"scope": self.scope.key, "skill_names": names, "enabled": enabled},
            },
        )

    def _require_own(self, info: SkillInfo) -> SkillInfo:
        """本作用域只管自己这一层的 Skill；继承来的条目只读。"""

        if info.scope.key != self.scope.key:
            raise ValueError("这个 Skill 继承自系统级，请在系统设置中修改")
        return info
