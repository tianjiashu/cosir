"""Skill 与 MCP 配置的作用域：系统级或某个 workspace。

两个作用域在各自的 ``.cosir`` 下使用完全相同的布局（``skills/``、``config/skills.json``、
``config/mcp_servers.json``），所以路径、作用域键与「Agent 看到的分层」都从这一个值对象取，
Skill 目录、MCP 配置、本机测试记录、连接池和目录变更通知不各自拼接。

Agent 看到的是分层合并结果：系统层在下、当前 workspace 层在上，同名时 workspace 覆盖系统
（与 Agent Team 的规则一致）。合并规则由各能力的读取模块实现，这里只提供层序。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.utils.path.system_cosir import (
    system_cosir_dir,
    system_mcp_servers_file,
    system_skills_config_file,
    system_skills_dir,
)
from app.utils.path.workspace_cosir import (
    workspace_cosir_dir,
    workspace_mcp_servers_file,
    workspace_skills_config_file,
    workspace_skills_dir,
)
from app.utils.workspace_scope import SYSTEM_SCOPE, normalize_scope_path

ExtensionScopeKind = Literal["system", "workspace"]


@dataclass(frozen=True)
class ExtensionScope:
    """一个 Skill / MCP 配置作用域；``workspace_root`` 为 ``None`` 即系统级。"""

    workspace_root: Path | None = None

    @classmethod
    def system(cls) -> ExtensionScope:
        return cls(None)

    @classmethod
    def workspace(cls, workspace_root: str | Path) -> ExtensionScope:
        return cls(Path(workspace_root))

    @staticmethod
    def visible_layers(workspace_root: str | Path | None) -> tuple[ExtensionScope, ...]:
        """Agent 或配置页看到的分层，按覆盖顺序排列（后者覆盖前者）。"""

        if workspace_root is None:
            return (ExtensionScope.system(),)
        return (ExtensionScope.system(), ExtensionScope.workspace(workspace_root))

    @property
    def kind(self) -> ExtensionScopeKind:
        return "system" if self.workspace_root is None else "workspace"

    @property
    def key(self) -> str:
        """进程内稳定的作用域键：系统级为 ``system``，workspace 为归一化根路径。"""

        if self.workspace_root is None:
            return SYSTEM_SCOPE
        return normalize_scope_path(self.workspace_root)

    @property
    def cosir_dir(self) -> Path:
        """本作用域的 ``.cosir`` 根：配置文件写入的受信任根，也是导入 / 删除暂存目录的父目录。"""

        if self.workspace_root is None:
            return system_cosir_dir()
        return workspace_cosir_dir(self.workspace_root)

    @property
    def skills_dir(self) -> Path:
        if self.workspace_root is None:
            return system_skills_dir()
        return workspace_skills_dir(self.workspace_root)

    @property
    def skills_config_file(self) -> Path:
        if self.workspace_root is None:
            return system_skills_config_file()
        return workspace_skills_config_file(self.workspace_root)

    @property
    def mcp_servers_file(self) -> Path:
        if self.workspace_root is None:
            return system_mcp_servers_file()
        return workspace_mcp_servers_file(self.workspace_root)
