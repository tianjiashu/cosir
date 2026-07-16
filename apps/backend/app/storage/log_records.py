"""日志查询与持久化记录结构。"""

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


LogSortOrder = Literal["asc", "desc"]


@dataclass(frozen=True)
class LogEntryRecord:
    """表示 SQLite 中的一条结构化日志。

    参数:
        ts: UTC RFC3339 日志时间。
        level: Python 标准日志级别。
        logger: 统一 logger 名称。
        trace_id: 唯一链路关联键。
        caller: 调用位置 ``模块:类.方法:行号``。
        event: 稳定英文事件名。
        msg: 中文可读消息。
        data: 结构化业务字段。
        error: 嵌套错误块，正常为 None。
        truncated: 是否发生截断。

    返回:
        不可变日志记录。

    异常:
        无。

    副作用:
        无。
    """

    ts: str
    level: str
    logger: str
    trace_id: str
    caller: str
    event: str
    msg: str
    data: dict[str, Any] = field(default_factory=dict)
    error: Optional[dict[str, str]] = None
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 可返回的字典。

        参数:
            无。

        返回:
            包含 9 个统一日志字段的字典。

        异常:
            无。

        副作用:
            无。
        """
        return {
            "ts": self.ts,
            "level": self.level,
            "logger": self.logger,
            "trace_id": self.trace_id,
            "caller": self.caller,
            "event": self.event,
            "msg": self.msg,
            "data": self.data,
            "error": self.error,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class LogQuery:
    """表示日志查询参数。

    参数:
        trace_id: 可选 trace 过滤条件（唯一链路键）。
        level: 可选日志级别过滤条件。
        event_name: 可选事件名过滤条件。
        start_time: 可选 UTC RFC3339 起始时间。
        end_time: 可选 UTC RFC3339 结束时间。
        limit: 最大返回数量。
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
    start_time: str = ""
    end_time: str = ""
    limit: int = 200
    order: LogSortOrder = "asc"


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
