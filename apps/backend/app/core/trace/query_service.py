"""Trace 查询用例服务。"""

from datetime import date
from pathlib import Path
from typing import Any

from app.config.logging import list_log_files
from app.config.logging import query_log_files
from app.storage.crud.trace import TraceStore


class TraceQueryService:
    """组合 TraceStore 与 JSONL 日志文件查询。"""

    def __init__(self, store: TraceStore, log_dir: Path) -> None:
        """初始化查询服务。

        参数:
            store: Trace SQLite 存储。
            log_dir: JSONL 日期日志目录。

        返回:
            无。

        异常:
            无。

        副作用:
            保存依赖引用。
        """

        self._store = store
        self._log_dir = log_dir

    def list_events(self, trace_id: str = "", run_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        """查询 trace event。

        参数:
            trace_id: 可选 trace 过滤条件。
            run_id: 可选 run 过滤条件。
            limit: 最大返回数量。

        返回:
            可 JSON 序列化的事件列表。

        异常:
            ValueError: 如果 limit 非法。

        副作用:
            读取 SQLite。
        """

        return [event.to_dict() for event in self._store.list_events(trace_id=trace_id, run_id=run_id, limit=limit)]

    def list_traces(self, limit: int = 100) -> list[dict[str, Any]]:
        """返回 trace 摘要列表。

        参数:
            limit: 最大返回数量。

        返回:
            trace 摘要字典列表。

        异常:
            ValueError: 如果 limit 非法。

        副作用:
            读取 SQLite。
        """

        return self._store.list_trace_summaries(limit=limit)

    def get_trace_summary(self, trace_id: str) -> dict[str, Any]:
        """返回单个 trace 详情摘要。

        参数:
            trace_id: Trace 标识。

        返回:
            包含 summary、events、spans 和 logs 的字典。

        异常:
            KeyError: 如果 trace 不存在。

        副作用:
            读取 SQLite 和 JSONL 日志文件。
        """

        summary = self._store.get_trace_summary(trace_id)
        return {
            **summary,
            "events": self.list_events(trace_id=trace_id),
            "spans": self.list_spans(trace_id=trace_id),
            "logs": self.list_logs(trace_id=trace_id),
        }

    def list_spans(self, trace_id: str = "", run_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        """查询 trace span。

        参数:
            trace_id: 可选 trace 过滤条件。
            run_id: 可选 run 过滤条件。
            limit: 最大返回数量。

        返回:
            可 JSON 序列化的 span 列表。

        异常:
            ValueError: 如果 limit 非法。

        副作用:
            读取 SQLite。
        """

        return [span.to_dict() for span in self._store.list_spans(trace_id=trace_id, run_id=run_id, limit=limit)]

    def list_logs(
        self,
        trace_id: str = "",
        level: str = "",
        log_date: date | None = None,
        start_time: str = "",
        end_time: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """查询 JSONL 日志文件。

        参数:
            trace_id: 可选 trace 过滤条件（唯一链路键，D4）。
            level: 可选日志级别过滤条件。
            log_date: 可选精确日期过滤条件。
            start_time: 可选起始 ISO 时间。
            end_time: 可选结束 ISO 时间。
            limit: 最大返回数量。

        返回:
            匹配的 JSONL 日志行。

        异常:
            ValueError: 如果 limit 非法。

        副作用:
            读取日志文件。
        """

        log_files = list_log_files(
            self._log_dir,
            log_date=log_date,
            start_time=start_time,
            end_time=end_time,
        )
        return query_log_files(
            log_files,
            trace_id=trace_id,
            level=level,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    def get_run_trace(self, run_id: str) -> dict[str, Any]:
        """返回 run 对应的 trace 摘要。

        参数:
            run_id: Durable Run 标识。

        返回:
            包含 events、spans 和 logs 的摘要字典。

        异常:
            ValueError: 如果 run_id 为空。

        副作用:
            读取 SQLite 和 JSONL 日志文件。
        """

        if not run_id:
            raise ValueError("run_id must not be blank")
        events = self.list_events(run_id=run_id)
        trace_id = events[0]["trace_id"] if events else ""
        return {
            "run_id": run_id,
            "trace_id": trace_id,
            "events": events,
            "spans": self.list_spans(run_id=run_id),
            "logs": self.list_logs(trace_id=trace_id),
        }
