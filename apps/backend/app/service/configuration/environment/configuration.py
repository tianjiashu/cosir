"""配置中心「环境变量」页的契约：字段的编辑与展示定义、展示分组、单个字段的变更意图。

只是值对象：不读写 ``.env``、不校验白名单与值类型（归 ``configuration_service``），字段目录在
``field_catalog``。
"""

from dataclasses import dataclass
from typing import Any, Literal


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


@dataclass(frozen=True)
class EnvironmentGroup:
    """配置中心中的一个展示分组，不承载配置文件或运行时状态。"""

    id: str
    label: str
    description: str


@dataclass(frozen=True)
class EnvironmentChange:
    """调用方对单个受管环境变量提出的变更意图。

    ``operation`` 为 ``replace``（写为新值）、``clear``（清除键）或 ``unchanged``（保持原值）；
    ``value`` 只在 ``replace`` 时由 ``EnvironmentConfigurationService`` 读取并校验。

    该对象只表达意图：不校验白名单字段名、不校验值类型、不读写 ``.env``，因此可在 API、service
    与测试之间直接传递。
    """

    operation: Literal["replace", "clear", "unchanged"]
    value: Any = None
