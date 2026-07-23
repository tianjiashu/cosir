"""日志查询结果的纯文本渲染。"""

from typing import Any

from app.models import LogEntryRecord


def render_log_entries(entries: list[LogEntryRecord]) -> str:
    """把结构化日志记录渲染为纯文本。

    参数:
        entries: 需要渲染的日志记录列表。

    返回:
        多行纯文本日志；没有记录时返回空字符串。

    异常:
        无。

    副作用:
        无。
    """
    return "\n".join(_render_entry(entry) for entry in entries)


def _flatten_kv(prefix: str, value: Any) -> list[str]:
    """把字典/列表递归扁平化为可读的 ``key=value`` 片段，避免使用花括号。

    参数:
        prefix: 当前层级的键前缀（如 ``data`` 或 ``data.user``）。
        value: 待扁平化的任意值。

    返回:
        扁平化后的 ``key=value`` 字符串列表；空容器返回空列表。

    异常:
        无。

    副作用:
        无。
    """
    if isinstance(value, dict):
        out: list[str] = []
        for k, v in value.items():
            out.extend(_flatten_kv(f"{prefix}.{k}" if prefix else str(k), v))
        return out
    if isinstance(value, list | tuple):
        out = []
        for i, v in enumerate(value):
            out.extend(_flatten_kv(f"{prefix}[{i}]", v))
        return out
    return [f"{prefix}={value}"]


def _render_entry(entry: LogEntryRecord) -> str:
    """渲染单条日志记录。

    参数:
        entry: 待渲染的日志记录。

    返回:
        单行日志文本；``data`` / ``error`` 以扁平 ``key=value`` 形式展示，不含花括号。

    异常:
        无。

    副作用:
        无。
    """
    parts = [
        entry.ts,
        entry.level,
        entry.logger,
        f"event={entry.event}",
    ]
    if entry.trace_id:
        parts.append(f"trace_id={entry.trace_id}")
    if entry.caller:
        parts.append(f"caller={entry.caller}")
    parts.append(f'msg="{entry.msg}"')
    if entry.data:
        parts.append(" ".join(_flatten_kv("data", entry.data)))
    if entry.error:
        parts.append(" ".join(_flatten_kv("error", entry.error)))
    return " ".join(parts)
