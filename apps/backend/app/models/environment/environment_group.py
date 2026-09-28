"""配置中心的展示分组值对象。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvironmentGroup:
    """配置中心中的一个展示分组，不承载配置文件或运行时状态。"""

    id: str
    label: str
    description: str
