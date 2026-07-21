"""日志查询结果值对象。

单一职责：承载日志查询的不可变结果（记录列表 + 纯文本渲染）。
不负责查询执行（由 ``core/logs/query_service`` 负责）。
"""

from dataclasses import dataclass
from typing import Any

from app.models.log_entry_record import LogEntryRecord


@dataclass(frozen=True)
class LogQueryResult:
    """表示日志查询结果。

    参数:
        entries: 结构化日志记录列表。
        text: 可直接展示的纯文本日志。

    返回:
        不可变查询结果。

    异常:
        无。

    副作用:
        无。
    """

    entries: list[LogEntryRecord]
    text: str

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 响应字典。

        参数:
            无。

        返回:
            包含 entries 与 text 的响应字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "text": self.text,
        }
