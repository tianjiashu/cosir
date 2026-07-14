"""工具目录的可发现性元数据。"""

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class ToolCatalogRecord:
    """描述工具可被模型选择的元数据。

    参数:
        name: 稳定工具名。
        namespace: 工具族命名空间。
        short_description: 用于候选列表的简短说明。
        tags: 用于检索的语义标签。
        permission: 声明权限。
        risk_level: 工具风险等级。
        visible_by_default: 是否可作为默认候选。

    返回:
        不可变工具目录记录。

    异常:
        无。

    副作用:
        无。
    """

    name: str
    namespace: str
    short_description: str
    tags: Sequence[str] = field(default_factory=tuple)
    permission: str = ""
    risk_level: str = "low"
    visible_by_default: bool = True
