"""结构化 JSONL 文件日志格式与查询。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any

from app.core.trace.redaction import redact_value
from app.config.logging.record_mapper import _contains_truncation, map_log_record


@dataclass(frozen=True)
class JsonlLogLine:
    """表示一行结构化日志。

    参数:
        ts: UTC 日志时间。
        level: 日志级别。
        logger_name: logger 名称。
        event_name: 稳定事件名。
        message: 人类可读消息。
        attributes: 结构化附加字段。
        trace_id: 一次前端用户操作触发的完整链路标识。
        run_id: 可选 run 标识。
        task_id: 可选 task 标识。
        span_id: 可选 span 标识。
        event_id: 可选事件标识。
        step_id: 可选步骤标识。
        tool_call_id: 可选工具调用标识。
        approval_id: 可选审批标识。
        error_type: 可选异常类型。
        error_message: 可选异常消息。
        stack: 可选异常堆栈。
        truncated: 是否发生过字段截断。

    返回:
        不可变 JSONL 日志行。

    异常:
        无。

    副作用:
        默认 ts 会读取系统时钟。
    """

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
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入 JSONL 的字典。

        参数:
            无。

        返回:
            已脱敏的日志字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "ts": self.ts.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": self.level,
            "logger_name": self.logger_name,
            "event_name": self.event_name,
            "message": self.message,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "span_id": self.span_id,
            "event_id": self.event_id,
            "step_id": self.step_id,
            "tool_call_id": self.tool_call_id,
            "approval_id": self.approval_id,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "stack": self.stack,
            "attributes": redact_value(self.attributes),
            "truncated": self.truncated,
        }


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

        mapped = map_log_record(record)
        line = JsonlLogLine(
            ts=datetime.fromisoformat(mapped.ts.replace("Z", "+00:00")),
            level=mapped.level,
            logger_name=mapped.logger_name,
            event_name=mapped.event_name,
            message=mapped.message,
            attributes=mapped.attributes,
            trace_id=mapped.trace_id,
            run_id=mapped.run_id,
            task_id=mapped.task_id,
            span_id=mapped.span_id,
            event_id=mapped.event_id,
            step_id=mapped.step_id,
            tool_call_id=mapped.tool_call_id,
            approval_id=mapped.approval_id,
            error_type=mapped.error_type,
            error_message=mapped.error_message,
            stack=mapped.stack,
            truncated=mapped.truncated,
        )
        redacted = line.to_dict()
        if _contains_truncation(redacted):
            line = JsonlLogLine(
                ts=line.ts,
                level=line.level,
                logger_name=line.logger_name,
                event_name=line.event_name,
                message=line.message,
                attributes=line.attributes,
                trace_id=line.trace_id,
                run_id=line.run_id,
                task_id=line.task_id,
                span_id=line.span_id,
                event_id=line.event_id,
                step_id=line.step_id,
                tool_call_id=line.tool_call_id,
                approval_id=line.approval_id,
                error_type=line.error_type,
                error_message=line.error_message,
                stack=line.stack,
                truncated=True,
            )
        return json.dumps(line.to_dict(), ensure_ascii=False, sort_keys=True)


def query_log_file(
    log_file: Path,
    trace_id: str = "",
    run_id: str = "",
    level: str = "",
    start_time: str = "",
    end_time: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """从 JSONL 文件中查询日志行。

    参数:
        log_file: JSONL 日志文件路径。
        trace_id: 可选 trace 过滤条件。
        run_id: 可选 run 过滤条件。
        level: 可选日志级别过滤条件。
        start_time: 可选起始 ISO 时间，包含边界。
        end_time: 可选结束 ISO 时间，包含边界。
        limit: 最大返回行数。

    返回:
        按文件顺序返回的日志字典列表。

    异常:
        ValueError: 如果 limit 小于 1。

    副作用:
        读取日志文件；文件不存在时返回空列表。
    """

    if limit < 1:
        raise ValueError("limit must be greater than zero")
    if not log_file.exists():
        return []
    matched: list[dict[str, Any]] = []
    with log_file.open("r", encoding="utf-8") as file:
        for line in file:
            item = _parse_line(line)
            if item is None or not _matches(
                item,
                trace_id=trace_id,
                run_id=run_id,
                level=level,
                start_time=start_time,
                end_time=end_time,
            ):
                continue
            matched.append(item)
            if len(matched) >= limit:
                break
    return matched


def query_log_files(
    log_files: list[Path],
    trace_id: str = "",
    run_id: str = "",
    level: str = "",
    start_time: str = "",
    end_time: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """从多个 JSONL 日志文件中查询日志行。

    参数:
        log_files: 按查询顺序排列的 JSONL 日志文件路径。
        trace_id: 可选 trace 过滤条件。
        run_id: 可选 run 过滤条件。
        level: 可选日志级别过滤条件。
        start_time: 可选起始 ISO 时间，包含边界。
        end_time: 可选结束 ISO 时间，包含边界。
        limit: 最大返回行数。

    返回:
        按文件顺序聚合后的日志字典列表。

    异常:
        ValueError: 如果 limit 小于 1。

    副作用:
        读取多个日志文件；不存在的文件由单文件查询函数返回空列表。
    """

    if limit < 1:
        raise ValueError("limit must be greater than zero")
    matched: list[dict[str, Any]] = []
    for log_file in log_files:
        remaining = limit - len(matched)
        if remaining <= 0:
            break
        matched.extend(
            query_log_file(
                log_file,
                trace_id=trace_id,
                run_id=run_id,
                level=level,
                start_time=start_time,
                end_time=end_time,
                limit=remaining,
            )
        )
    return matched


def _parse_line(line: str) -> dict[str, Any] | None:
    """解析单行 JSONL。

    参数:
        line: 日志文件中的原始行。

    返回:
        解析成功时返回字典，空行或非法 JSON 返回 None。

    异常:
        无。

    副作用:
        无。
    """

    if not line.strip():
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _matches(
    item: dict[str, Any],
    trace_id: str,
    run_id: str,
    level: str,
    start_time: str,
    end_time: str,
) -> bool:
    """判断日志行是否匹配过滤条件。

    参数:
        item: 已解析日志行。
        trace_id: trace 过滤条件。
        run_id: run 过滤条件。
        level: level 过滤条件。
        start_time: 起始时间过滤条件。
        end_time: 结束时间过滤条件。

    返回:
        全部非空过滤条件都匹配时返回 True。

    异常:
        无。

    副作用:
        无。
    """

    if trace_id and item.get("trace_id") != trace_id:
        return False
    if run_id and item.get("run_id") != run_id:
        return False
    if level and str(item.get("level", "")).lower() != level.lower():
        return False
    item_ts = str(item.get("ts", ""))
    item_time = _parse_iso_datetime(item_ts)
    start = _parse_iso_datetime(start_time)
    end = _parse_iso_datetime(end_time)
    if start is not None and (item_time is None or item_time < start):
        return False
    if end is not None and (item_time is None or item_time > end):
        return False
    return True


def _parse_iso_datetime(value: str) -> datetime | None:
    """解析 ISO 日期时间并归一化为 UTC。

    参数:
        value: ISO 日期时间文本，允许为空。

    返回:
        非空合法文本对应的 UTC datetime；空字符串返回 None。

    异常:
        ValueError: 如果非空文本不是合法 ISO 日期时间。

    副作用:
        无。
    """

    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)



