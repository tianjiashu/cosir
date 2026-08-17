"""日志查询结果值对象。

单一职责：承载日志查询的不可变结果（记录列表 + 纯文本渲染）。
不负责查询执行（由 ``core/logs/query_service`` 负责）。
"""

from dataclasses import dataclass, field
from typing import Any

from app.models.log_entry_record import LogEntryRecord


@dataclass(frozen=True)
class LogQueryResult:
    """表示日志查询结果。

    参数:
        entries: 结构化日志记录列表。
        text: 可直接展示的纯文本日志。
        total: 匹配查询条件的总记录数（不受 limit/offset 影响）。
        has_more: 当前页之后是否还有更多记录。
        level_counts: 各级别日志计数（忽略 level 过滤、含其余过滤条件），供前端
            展示级别分布并联动级别筛选。

    返回:
        不可变查询结果。

    异常:
        无。

    副作用:
        无。
    """

    entries: list[LogEntryRecord]
    text: str
    total: int = 0
    has_more: bool = False
    level_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 响应字典。

        参数:
            无。

        返回:
            包含 entries、text、total、has_more 与 level_counts 的响应字典；total / has_more
            供前端分页判断总页数与是否存在后续页，level_counts 供前端展示级别分布。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "text": self.text,
            "total": self.total,
            "has_more": self.has_more,
            "level_counts": self.level_counts,
        }
