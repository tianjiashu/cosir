"""日志查询结果的纯文本渲染。"""

from app.storage.log_records import LogEntryRecord


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


def _render_entry(entry: LogEntryRecord) -> str:
    """渲染单条日志记录。

    参数:
        entry: 待渲染的日志记录。

    返回:
        单行日志文本。

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
        parts.append(f"data={entry.data}")
    if entry.error:
        parts.append(f"error={entry.error}")
    return " ".join(parts)
