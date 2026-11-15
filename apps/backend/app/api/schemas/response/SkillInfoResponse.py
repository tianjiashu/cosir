"""Skill 列表项响应。"""

from typing import Literal

from pydantic import BaseModel


class SkillInfoResponse(BaseModel):
    """一个可见 Skill。

    ``scope`` 是它所在的作用域，workspace 配置页里 ``scope == "system"`` 的条目是继承来的，只读。
    ``kind`` 是身份：``cosir`` 专属（只有 cosir 读）或 ``shared`` 共享（Codex 等也读）；对 Agent 两者平等。
    ``overrides`` 表示它遮住了优先级更低、内容不同的同名 Skill；``warnings`` 是不合标准但不妨碍使用的
    问题；``manual_only`` 表示只供用户手动选用；``has_scripts`` 表示带 ``scripts/`` 目录。
    """

    name: str
    description: str
    enabled: bool
    scope: Literal["system", "workspace"]
    kind: Literal["cosir", "shared"]
    overrides: bool
    warnings: list[str]
    manual_only: bool
    has_scripts: bool
