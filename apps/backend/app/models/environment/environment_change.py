"""环境配置变更意图值对象。"""

from dataclasses import dataclass
from typing import Any, Literal


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
