"""Skill 列表项响应。"""

from typing import Literal

from pydantic import BaseModel


class SkillInfoResponse(BaseModel):
    """一个可见 Skill：``scope`` 是它所在的作用域，``overrides`` 表示它遮住了系统级同名 Skill。

    workspace 配置页里 ``scope == "system"`` 的条目是继承来的，只读。
    """

    name: str
    description: str
    enabled: bool
    scope: Literal["system", "workspace"]
    overrides: bool
