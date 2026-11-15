"""Skill 根目录：每个作用域两种身份的目录，以及它们之间的优先级（唯一事实源）。

- ``cosir``：cosir 专属（系统级 ``<数据根>/.cosir/skills``，workspace ``<root>/.cosir/skills``），只有 cosir
  读；导入落在这里，以后的内置 Skill 也归这一侧。
- ``shared``：共享（``.agents/skills``，系统级在用户主目录），Codex 等工具都读；cosir 只读、只启停。

对 Agent 两种身份完全平等：它看到的是合并后的平铺目录，不知道来源。优先级只在同名且指纹不同（真冲突）时
决定用哪份：workspace 压过系统级，同一作用域里 ``cosir`` 压过 ``shared``。合并规则在 ``SkillCatalog.merge``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.service.configuration.extension_scope import ExtensionScope
from app.utils.path.shared_skills import (
    home_relative,
    system_shared_skills_dir,
    workspace_shared_skills_dir,
)
from app.utils.path.system_cosir import system_cosir_skills_dir
from app.utils.path.workspace_cosir import workspace_cosir_skills_dir

SkillRootKind = Literal["cosir", "shared"]

# 同一作用域内从低到高；跨作用域的层序取 ``ExtensionScope.visible_layers``。
_KINDS_LOW_TO_HIGH: tuple[SkillRootKind, ...] = ("shared", "cosir")


@dataclass(frozen=True)
class SkillRoot:
    """一个作用域里一种身份的 Skill 目录；不检查目录是否存在。"""

    scope: ExtensionScope
    kind: SkillRootKind

    @staticmethod
    def visible_from(workspace_root: str | Path | None) -> tuple[SkillRoot, ...]:
        """某个工作区（``None`` 为系统级配置页）看得到的全部根目录，按优先级从低到高。"""

        return tuple(
            SkillRoot(scope, kind)
            for scope in ExtensionScope.visible_layers(workspace_root)
            for kind in _KINDS_LOW_TO_HIGH
        )

    @property
    def directory(self) -> Path:
        root = self.scope.workspace_root
        if self.kind == "cosir":
            return system_cosir_skills_dir() if root is None else workspace_cosir_skills_dir(root)
        return system_shared_skills_dir() if root is None else workspace_shared_skills_dir(root)

    @property
    def location(self) -> str:
        """给人看的位置：系统级写成 ``~/…``（Linux 的专属目录在应用数据目录下），workspace 写成相对工作区根。"""

        root = self.scope.workspace_root
        if root is None:
            return home_relative(self.directory)
        return self.directory.relative_to(root).as_posix()
