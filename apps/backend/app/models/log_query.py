"""日志查询参数值对象。

单一职责：承载日志查询的不可变参数。
不负责查询执行（由 ``core/logs/query_service`` 负责）。
"""

from dataclasses import dataclass
from typing import Literal

LogSortOrder = Literal["asc", "desc"]


@dataclass(frozen=True)
class LogQuery:
    """表示日志查询参数。

    参数:
        trace_id: 可选 trace 过滤条件（唯一链路键）。
        level: 可选日志级别过滤条件。
        event_name: 可选事件名过滤条件。
        keyword: 可选关键词，对 ``msg`` 字段做子串（LIKE）匹配。
        start_time: 可选 UTC RFC3339 起始时间。
        end_time: 可选 UTC RFC3339 结束时间。
        limit: 最大返回数量。
        offset: 跳过的记录数量（分页起点）。
        order: 返回排序方向。

    返回:
        不可变查询参数。

    异常:
        无。

    副作用:
        无。
    """

    trace_id: str = ""
    level: str = ""
    event_name: str = ""
    keyword: str = ""
    start_time: str = ""
    end_time: str = ""
    limit: int = 200
    offset: int = 0
    order: LogSortOrder = "asc"
