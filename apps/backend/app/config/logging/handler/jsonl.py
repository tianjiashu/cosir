"""结构化 JSONL 文件日志格式与查询。"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config.logging.record_mapper import LogError, map_log_record


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
def query_log_file(
    log_file: Path,
    trace_id: str = "",
    level: str = "",
    start_time: str = "",
    end_time: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """从 JSONL 文件中查询日志行。

    参数:
        log_file: JSONL 日志文件路径。
        trace_id: 可选 trace 过滤条件（唯一链路键，D4）。
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
    level: str = "",
    start_time: str = "",
    end_time: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """从多个 JSONL 日志文件中查询日志行。

    参数:
        log_files: 按查询顺序排列的 JSONL 日志文件路径。
        trace_id: 可选 trace 过滤条件（唯一链路键，D4）。
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
                level=level,
                start_time=start_time,
                end_time=end_time,
                limit=remaining,
            )
        )
    return matched


def _parse_line(line: str) -> dict[str, Any] | None:
    """解析单行 JSONL，容忍旧格式字段缺失。

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
    level: str,
    start_time: str,
    end_time: str,
) -> bool:
    """判断日志行是否匹配过滤条件。

    参数:
        item: 已解析日志行。
        trace_id: trace 过滤条件（唯一链路键，D4）。
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
