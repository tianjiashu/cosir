"""日志查询业务服务。"""

from datetime import datetime, timezone
from typing import Any

from app.models import LogQuery, LogEntryRecord
from app.models import LogQueryResult
from app.storage.crud.log_crud import LogStore


class LogQueryService:
    """封装日志查询规则和文本渲染。"""

    def __init__(self, store: LogStore, max_limit: int = 1000) -> None:
        """初始化日志查询服务。

        参数:
            store: 日志 SQLite 存储。
            max_limit: API 允许的最大 limit。

        返回:
            无。

        异常:
            ValueError: 如果最大 limit 小于 1。

        副作用:
            保存依赖引用。
        """

        if max_limit < 1:
            raise ValueError("max_limit must be greater than zero")
        self._store = store
        self._max_limit = max_limit

    def query_by_trace(
            self,
            trace_id: str,
            level: str = "",
            start_time: str = "",
            end_time: str = "",
            limit: int = 200,
    ) -> LogQueryResult:
        """按 trace_id 查询完整链路日志。

        参数:
            trace_id: 必填 trace 标识（日志层唯一链路键）。
            level: 可选日志级别。
            start_time: 可选 UTC RFC3339 起始时间。
            end_time: 可选 UTC RFC3339 结束时间。
            limit: 最大返回数量。

        返回:
            包含结构化 entries 和纯文本 text 的结果。

        异常:
            ValueError: 如果 trace_id、limit 或时间格式非法。

        副作用:
            读取日志 SQLite。
        """

        if not trace_id.strip():
            raise ValueError("trace_id must not be blank")
        query = LogQuery(
            trace_id=trace_id.strip(),
            level=self._normalize_level(level),
            start_time=self._normalize_time(start_time),
            end_time=self._normalize_time(end_time),
            limit=self._normalize_limit(limit),
            order="asc",
        )
        return self._query(query)

    def recent(
            self,
            level: str = "",
            start_time: str = "",
            end_time: str = "",
            limit: int = 200,
    ) -> LogQueryResult:
        """查询最近日志。

        参数:
            level: 可选日志级别。
            start_time: 可选 UTC RFC3339 起始时间。
            end_time: 可选 UTC RFC3339 结束时间。
            limit: 最大返回数量。

        返回:
            包含结构化 entries 和纯文本 text 的结果。

        异常:
            ValueError: 如果 limit 或时间格式非法。

        副作用:
            读取日志 SQLite。
        """

        query = LogQuery(
            level=self._normalize_level(level),
            start_time=self._normalize_time(start_time),
            end_time=self._normalize_time(end_time),
            limit=self._normalize_limit(limit),
            order="desc",
        )
        return self._query(query)

    def _query(self, query: LogQuery) -> LogQueryResult:
        """执行查询并渲染纯文本。

        参数:
            query: 已校验的查询参数。

        返回:
            查询结果。

        异常:
            Exception: 如果底层日志存储查询失败。

        副作用:
            读取日志 SQLite。
        """

        entries = self._store.query(query)
        return LogQueryResult(entries=entries, text=LogQueryService.render_log_entries(entries))

    def _normalize_limit(self, limit: int) -> int:
        """校验并归一化 limit。

        参数:
            limit: 调用方传入的最大返回数量。

        返回:
            合法 limit。

        异常:
            ValueError: 如果 limit 越界。

        副作用:
            无。
        """

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        if limit > self._max_limit:
            raise ValueError(f"limit must be less than or equal to {self._max_limit}")
        return limit

    @staticmethod
    def _normalize_level(level: str) -> str:
        """归一化日志级别查询参数。

        参数:
            level: 原始级别文本。

        返回:
            大写后的 Python 标准日志级别；空字符串表示不筛选。

        异常:
            ValueError: 如果级别不受支持。

        副作用:
            无。
        """

        normalized = level.strip().upper()
        if not normalized:
            return ""
        if normalized == "WARN":
            normalized = "WARNING"
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        return normalized

    @staticmethod
    def _normalize_time(value: str) -> str:
        """校验并归一化 UTC RFC3339 时间。

        参数:
            value: 原始时间文本，允许为空。

        返回:
            空字符串或毫秒精度 UTC RFC3339 文本。

        异常:
            ValueError: 如果时间文本非法。

        副作用:
            无。
        """

        if not value.strip():
            return ""
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
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
        return "\n".join(LogQueryService._render_entry(entry) for entry in entries)


    @staticmethod
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
            parts.append(" ".join(LogQueryService._flatten_kv("data", entry.data)))
        if entry.error:
            parts.append(" ".join(LogQueryService._flatten_kv("error", entry.error)))
        return " ".join(parts)

    @staticmethod
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
                out.extend(LogQueryService._flatten_kv(f"{prefix}.{k}" if prefix else str(k), v))
            return out
        if isinstance(value, list | tuple):
            out = []
            for i, v in enumerate(value):
                out.extend(LogQueryService._flatten_kv(f"{prefix}[{i}]", v))
            return out
        return [f"{prefix}={value}"]