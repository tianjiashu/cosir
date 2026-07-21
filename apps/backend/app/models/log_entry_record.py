"""结构化日志条目值对象。

单一职责：承载 SQLite 中的一条结构化日志并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/log_crud`` 负责）。
"""

from dataclasses import dataclass, field
from typing import Any


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
    error: dict[str, str] | None = None
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
