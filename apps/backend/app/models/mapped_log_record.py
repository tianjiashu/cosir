"""将 Python LogRecord 映射为统一 9 字段日志结构。

映射逻辑收口在 :class:`MappedLogRecord` 的 :meth:`from_record` 工厂方法中；
:class:`LogError` 是映射产出的嵌套错误块，:data:`MAX_LOG_TEXT_LENGTH` 为
文本截断阈值常量。
"""

import logging
import re
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config.logging.filter.caller_filter import compute_caller
from app.trace_infra.redaction import redact_value

MAX_LOG_TEXT_LENGTH = 2000
EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class LogError:
    """表示嵌套错误现场块。

    参数:
        type: 异常类型名。
        message: 异常消息。
        stack: 异常堆栈文本。

    返回:
        不可变错误块。

    异常:
        无。

    副作用:
        无。
    """

    type: str = ""
    message: str = ""
    stack: str = ""


@dataclass(frozen=True)
class MappedLogRecord:
    """表示统一 9 字段日志结构。

    本值对象自带工厂方法 :meth:`from_record`，把 Python ``LogRecord`` 直接
    映射为统一的 9 字段结构，避免引入独立的无状态 Mapper 类。其余 ``_extract_*``
    / ``_truncate_text`` / ``_mark_truncated`` / ``_contains_truncation`` 均为
    ``@staticmethod`` 辅助步骤，不对外暴露。

    参数:
        ts: UTC RFC3339 日志时间。
        level: Python 标准日志级别名称。
        logger: 统一 logger 名称。
        trace_id: 唯一链路关联键。
        caller: 调用位置 ``模块:类.方法:行号``。
        event: 稳定英文事件名。
        msg: 中文可读消息。
        data: 结构化业务字段。
        error: 嵌套错误块，正常为 None。
        truncated: 是否发生字段截断。

    返回:
        不可变日志字段对象。

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
    error: LogError | None = None
    truncated: bool = False

    @classmethod
    def from_record(cls, record: logging.LogRecord) -> "MappedLogRecord":
        """把 Python ``LogRecord`` 映射为文件日志和 SQLite 共用字段。

        参数:
            record: Python logging 框架传入的日志记录。

        返回:
            已提取 event、msg、data、error、caller 的统一字段对象。

        异常:
            无。

        副作用:
            无。
        """

        data = cls._extract_data(record)
        event = cls._extract_event(record)
        msg = cls._extract_msg(record, event)
        error = cls._extract_error(record)
        mapped = cls(
            ts=cls._format_record_time(record.created),
            level=record.levelname,
            logger=record.name,
            trace_id=str(getattr(record, "trace_id", "") or ""),
            caller=str(getattr(record, "caller", "") or compute_caller(record)),
            event=event,
            msg=msg,
            data=data,
            error=error,
        )
        return cls._mark_truncated(mapped)

    @staticmethod
    def _format_record_time(created: float) -> str:
        """把 LogRecord 创建时间格式化为 UTC RFC3339 文本。

        参数:
            created: ``LogRecord.created`` 秒级时间戳。

        返回:
            使用毫秒精度和 ``Z`` 后缀的 UTC 时间文本。

        异常:
            无。

        副作用:
            无。
        """

        return (
            datetime.fromtimestamp(created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _extract_event(record: logging.LogRecord) -> str:
        """从日志消息提取稳定事件名。

        参数:
            record: Python logging 记录。

        返回:
            snake_case 事件名；无法提取时返回 ``log_event``。

        异常:
            无。

        副作用:
            无。
        """

        raw_message = str(record.msg or "")
        candidate = raw_message.strip().split(maxsplit=1)[0] if raw_message.strip() else ""
        if EVENT_NAME_PATTERN.match(candidate):
            return candidate
        return "log_event"

    @staticmethod
    def _extract_msg(record: logging.LogRecord, event: str) -> str:
        """提取中文可读消息，回退到稳定事件名。

        ``msg`` 是规范约定的 extra 键，由 ``install_msg_relocation`` 在 makeRecord
        阶段重定位到 ``display_message``（避免覆盖事件模板 ``record.msg``）；旧调用方
        也可能直接传 ``display_message``。两者皆缺时回退到稳定 event 名。

        参数:
            record: Python logging 记录。
            event: 已提取的稳定事件名，用作兜底。

        返回:
            截断后的 msg 文本。

        异常:
            无。

        副作用:
            无。
        """

        msg = str(getattr(record, "display_message", "") or "")
        if not msg:
            msg = event
        return MappedLogRecord._truncate_text(msg)

    @staticmethod
    def _extract_data(record: logging.LogRecord) -> dict[str, Any]:
        """从 LogRecord 提取非保留字段作为 data。

        参数:
            record: Python logging 记录。

        返回:
            已脱敏的业务字段字典；显式 ``data`` 与散落 extra 字段合并。

        异常:
            无。

        副作用:
            无。
        """

        ignored = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)))
        ignored.update(
            {
                "message",
                "asctime",
                "trace_id",
                "caller",
                "msg",
                "data",
                "error",
                "error_type",
                "error_message",
                "stack",
                "display_message",
            }
        )
        explicit = getattr(record, "data", None)
        data: dict[str, Any] = dict(explicit) if isinstance(explicit, dict) else {}
        for key, value in vars(record).items():
            if key in ignored:
                continue
            data[key] = value
        return redact_value(data, max_text_length=MAX_LOG_TEXT_LENGTH)

    @staticmethod
    def _extract_error(record: logging.LogRecord) -> LogError | None:
        """提取嵌套错误块。

        参数:
            record: Python logging 记录。

        返回:
            出错时返回 ``LogError``，否则返回 None。优先取 exc_info，
            其次取跨进程桥接写入的 error_type/error_message/stack 属性。

        异常:
            无。

        副作用:
            无。
        """

        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            stack = "".join(traceback.format_exception(*record.exc_info))
            return LogError(
                type=type(exc).__name__,
                message=MappedLogRecord._truncate_text(str(exc)),
                stack=MappedLogRecord._truncate_text(stack),
            )
        error_type = str(getattr(record, "error_type", "") or "")
        if error_type:
            return LogError(
                type=error_type,
                message=MappedLogRecord._truncate_text(
                    str(getattr(record, "error_message", "") or "")
                ),
                stack=MappedLogRecord._truncate_text(str(getattr(record, "stack", "") or "")),
            )
        return None

    @staticmethod
    def _truncate_text(value: str) -> str:
        """裁剪过长日志文本。

        参数:
            value: 原始文本。

        返回:
            未超限时返回原文，超限时返回带截断标记的文本。

        异常:
            无。

        副作用:
            无。
        """

        if "[TRUNCATED:" in value:
            # 已是带截断标记的二次输入（如跨进程桥接预截断后回传），
            # 不再重复截断，避免叠加多个 [TRUNCATED:] 标记。
            return value
        if len(value) <= MAX_LOG_TEXT_LENGTH:
            return value
        return f"{value[:MAX_LOG_TEXT_LENGTH]}...[TRUNCATED:{len(value)}]"

    @staticmethod
    def _mark_truncated(mapped: "MappedLogRecord") -> "MappedLogRecord":
        """根据字段内容标记是否发生截断。

        参数:
            mapped: 已映射的日志字段。

        返回:
            如果任意字段含截断标记，则返回 ``truncated=True`` 的副本。

        异常:
            无。

        副作用:
            无。
        """

        if not MappedLogRecord._contains_truncation(mapped.__dict__):
            return mapped
        return MappedLogRecord(**{**mapped.__dict__, "truncated": True})

    @staticmethod
    def _contains_truncation(value: Any) -> bool:
        """递归判断值中是否包含截断标记。

        参数:
            value: 任意待检查值。

        返回:
            包含截断标记时返回 True。

        异常:
            无。

        副作用:
            无。
        """

        if isinstance(value, str):
            return "[TRUNCATED:" in value
        if isinstance(value, dict):
            return any(MappedLogRecord._contains_truncation(item) for item in value.values())
        if isinstance(value, list | tuple | set):
            return any(MappedLogRecord._contains_truncation(item) for item in value)
        if hasattr(value, "__dict__"):
            return MappedLogRecord._contains_truncation(vars(value))
        return False
