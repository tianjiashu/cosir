"""将 Python LogRecord 映射为统一日志字段。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import re
import traceback
from typing import Any


MAX_LOG_TEXT_LENGTH = 2000
EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class MappedLogRecord:
    """表示从 ``logging.LogRecord`` 提取出的统一日志字段。

    参数:
        ts: UTC RFC3339 日志时间文本。
        level: Python 标准日志级别名称。
        logger_name: logger 名称。
        event_name: 稳定事件名。
        message: 人类可读展示文本。
        attributes: 可变业务字段。
        trace_id: 前端操作 trace 标识。
        run_id: Durable Run 标识。
        task_id: 任务标识。
        span_id: trace span 标识。
        event_id: 事件标识。
        step_id: 步骤标识。
        tool_call_id: 工具调用标识。
        approval_id: 审批标识。
        error_type: 异常类型。
        error_message: 异常消息。
        stack: 异常栈文本。
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
    logger_name: str
    event_name: str
    message: str
    attributes: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    run_id: str = ""
    task_id: str = ""
    span_id: str = ""
    event_id: str = ""
    step_id: str = ""
    tool_call_id: str = ""
    approval_id: str = ""
    error_type: str = ""
    error_message: str = ""
    stack: str = ""
    truncated: bool = False


def map_log_record(record: logging.LogRecord) -> MappedLogRecord:
    """把 Python ``LogRecord`` 映射为文件日志和 SQLite 共用字段。

    参数:
        record: Python logging 框架传入的日志记录。

    返回:
        已提取事件名、展示文本、上下文、错误栈和 attributes 的统一字段对象。

    异常:
        无。

    副作用:
        无。
    """

    attributes = _extract_attributes(record)
    event_name = _extract_event_name(record)
    display_message = attributes.pop("display_message", "")
    message = _truncate_text(str(display_message or event_name))
    error_type = str(getattr(record, "error_type", "") or "")
    error_message = ""
    stack = ""
    if record.exc_info and record.exc_info[1] is not None:
        error_type = type(record.exc_info[1]).__name__
        error_message = _truncate_text(str(record.exc_info[1]))
        stack = _truncate_text("".join(traceback.format_exception(*record.exc_info)))
    else:
        error_message = _truncate_text(str(getattr(record, "error_message", "") or ""))
        stack = _truncate_text(str(getattr(record, "stack", "") or ""))
    mapped = MappedLogRecord(
        ts=_format_record_time(record.created),
        level=record.levelname,
        logger_name=record.name,
        event_name=event_name,
        message=message,
        attributes=attributes,
        trace_id=str(getattr(record, "trace_id", "") or ""),
        run_id=str(getattr(record, "run_id", "") or ""),
        task_id=str(getattr(record, "task_id", "") or ""),
        span_id=str(getattr(record, "span_id", "") or ""),
        event_id=str(getattr(record, "event_id", "") or ""),
        step_id=str(getattr(record, "step_id", "") or ""),
        tool_call_id=str(getattr(record, "tool_call_id", "") or ""),
        approval_id=str(getattr(record, "approval_id", "") or ""),
        error_type=error_type,
        error_message=error_message,
        stack=stack,
    )
    return _mark_truncated(mapped)


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

    return datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _extract_event_name(record: logging.LogRecord) -> str:
    """从原始日志消息中提取稳定事件名。

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


def _extract_attributes(record: logging.LogRecord) -> dict[str, Any]:
    """从 LogRecord 提取非标准、非关联字段。

    参数:
        record: Python logging 记录。

    返回:
        可写入 attributes 的业务字段字典。

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
            "run_id",
            "task_id",
            "span_id",
            "event_id",
            "step_id",
            "tool_call_id",
            "approval_id",
            "error_type",
            "error_message",
            "stack",
        }
    )
    return {key: value for key, value in vars(record).items() if key not in ignored}


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

    if len(value) <= MAX_LOG_TEXT_LENGTH:
        return value
    return f"{value[:MAX_LOG_TEXT_LENGTH]}...[TRUNCATED:{len(value)}]"


def _mark_truncated(mapped: MappedLogRecord) -> MappedLogRecord:
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

    if not _contains_truncation(mapped.__dict__):
        return mapped
    return MappedLogRecord(**{**mapped.__dict__, "truncated": True})


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
        return any(_contains_truncation(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_truncation(item) for item in value)
    return False
