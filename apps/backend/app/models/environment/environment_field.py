"""单个受管环境变量的编辑与展示契约。"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class EnvironmentField:
    """环境变量的编辑与展示契约。

    该定义同时服务于 service 的值校验和配置中心 UI 的元数据投影；它不保存当前环境值，
    也不改变 `.env` 的持久化格式。``component`` 只描述前端应使用的控件，不能替代
    ``value_type`` 的后端校验。
    """

    name: str
    value_type: str
    component: Literal["input", "password", "checkbox", "select"]
    group_id: str
    label: str
    secret: bool
    default: str | int | bool | None
    options: tuple[tuple[str, str], ...] = ()
    placeholder: str | None = None
    clearable: bool = True
    minimum: int | None = None
    maximum: int | None = None
