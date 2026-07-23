import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.models.mapped_log_record import MappedLogRecord, LogError


class JsonlFormatter(logging.Formatter):
    """将 Python LogRecord 格式化为单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录。

        参数:
            record: Python logging 传入的记录。

        返回:
            单行 JSON 字符串。

        异常:
            TypeError: 如果日志属性无法序列化且无法字符串化。

        副作用:
            调用父类格式化异常文本。
        """
        mapped = MappedLogRecord.from_record(record)
        line = JsonlLogLine(
            ts=datetime.fromisoformat(mapped.ts.replace("Z", "+00:00")),
            level=mapped.level,
            logger=mapped.logger,
            trace_id=mapped.trace_id,
            caller=mapped.caller,
            event=mapped.event,
            msg=mapped.msg,
            data=mapped.data,
            error=mapped.error,
            truncated=mapped.truncated,
        )
        return json.dumps(line.to_dict(), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class JsonlLogLine:
    """表示一行结构化日志。

    参数:
        ts: UTC 日志时间。
        level: 日志级别。
        logger: 统一 logger 名称。
        trace_id: 唯一链路关联键。
        caller: 调用位置。
        event: 稳定事件名。
        msg: 中文可读消息。
        data: 结构化业务字段。
        error: 嵌套错误块。
        truncated: 是否发生过字段截断。

    返回:
        不可变 JSONL 日志行。

    异常:
        无。

    副作用:
        默认 ts 会读取系统时钟。
    """

    level: str
    logger: str
    trace_id: str
    caller: str
    event: str
    msg: str
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = field(default_factory=dict)
    error: LogError | None = None
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入 JSONL 的字典。

        参数:
            无。

        返回:
            已脱敏的 9 字段日志字典。

        异常:
            无。

        副作用:
            无。
        """
        return {
            "ts": self.ts.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": self.level,
            "logger": self.logger,
            "trace_id": self.trace_id,
            "caller": self.caller,
            "event": self.event,
            "msg": self.msg,
            "data": self.data,
            "error": self.error.__dict__ if self.error is not None else None,
            "truncated": self.truncated,
        }