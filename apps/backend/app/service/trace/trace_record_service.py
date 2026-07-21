"""Trace ledger 写入门面。"""

import logging
from typing import Any

from app.models.trace_context import TraceContext
from app.storage.crud.trace_crud import TraceStore
from app.trace_infra.event_names import canonical_event_name
from app.trace_infra.ids import new_span_id, new_trace_id
from app.models.trace_event_record import TraceEventRecord
from app.models.trace_span_record import TraceSpanRecord
from app.trace_infra.redaction import redact_value


class TraceRecordService:
    """封装 trace event 与 span 写入。"""

    def __init__(self, store: TraceStore, logger: logging.Logger) -> None:
        """初始化 TraceRecorder。

        参数:
            store: TraceStore 实例。
            logger: 诊断日志器。

        返回:
            无。

        异常:
            无。

        副作用:
            保存依赖引用。
        """

        self._store = store
        self._logger = logger
        self._task_trace_ids: dict[str, str] = {}

    def context_for_task(self, task_id: str, run_id: str = "") -> TraceContext:
        """返回任务对应的 trace 上下文。

        参数:
            task_id: 任务标识。
            run_id: 可选 Durable Run 标识。

        返回:
            带稳定 trace_id 的 TraceContext。

        异常:
            无。

        副作用:
            首次访问任务时生成并缓存 trace_id。
        """

        trace_id = self._task_trace_ids.setdefault(task_id, new_trace_id())
        return TraceContext(trace_id=trace_id, task_id=task_id, run_id=run_id)

    def delete_task_traces(self, task_ids: list[str]) -> None:
        """删除任务集合下的 trace 记录并清理内存 trace 映射。

        参数:
            task_ids: 需要删除的任务标识符列表。

        返回:
            无。

        异常:
            无。底层删除失败会向调用方抛出，保持删除操作可审计。

        副作用:
            删除 trace store 中的关联记录，并移除内存缓存。
        """

        if not task_ids:
            return
        try:
            self._store.delete_by_task_ids(task_ids)
        except Exception:
            self._logger.exception(
                "task_traces_delete_failed",
                extra={
                    "msg": "删除任务集合下的 trace 记录失败",
                    "data": {"task_count": len(task_ids), "operation": "delete_task_traces"},
                },
            )
            raise
        for task_id in task_ids:
            self._task_trace_ids.pop(task_id, None)
        self._logger.info(
            "task_traces_deleted",
            extra={
                "msg": "任务集合下的 trace 记录已删除",
                "data": {"task_count": len(task_ids)},
            },
        )

    def record_event(
        self,
        context: TraceContext,
        event_type: str,
        payload: dict[str, Any],
        source: str,
        level: str = "info",
    ) -> TraceEventRecord | None:
        """追加 trace ledger 事件。

        参数:
            context: 当前 trace 上下文。
            event_type: 输入事件名，会归一化为 canonical 事件名。
            payload: 事件载荷。
            source: 事件来源模块。
            level: 事件级别。

        返回:
            写入成功时返回 TraceEventRecord；写入失败时返回 None。

        异常:
            无。写入失败会记录日志但不破坏主流程。

        副作用:
            尝试向 TraceStore 写入事件。
        """

        try:
            run_id = context.run_id or "unbound"
            event = TraceEventRecord(
                trace_id=context.trace_id,
                run_id=run_id,
                task_id=context.task_id,
                span_id=context.span_id,
                parent_span_id=context.parent_span_id,
                sequence_no=0,
                event_type=canonical_event_name(event_type),
                source=source,
                level=level,
                payload=redact_value(payload),
            )
            return self._store.append_event(event)
        except Exception as exc:
            self._logger.warning(
                "trace_event_write_failed",
                extra={
                    "msg": f"Trace 事件写入失败，event_type={event_type}",
                    "data": {
                        "task_id": context.task_id,
                        "event_type": event_type,
                        "error": str(exc),
                    },
                },
            )
            return None

    def start_span(
        self,
        context: TraceContext,
        name: str,
        kind: str,
        attributes: dict[str, Any] | None = None,
    ) -> TraceContext:
        """开始一个 span 并返回该 span 上下文。

        参数:
            context: 父级 trace 上下文。
            name: span 名称。
            kind: span 类型。
            attributes: 可选 span 属性。

        返回:
            新 span 对应的 TraceContext。

        异常:
            无。写入失败会记录日志，仍返回上下文。

        副作用:
            尝试向 TraceStore 写入 span。
        """

        span_context = TraceContext(
            trace_id=context.trace_id,
            task_id=context.task_id,
            run_id=context.run_id,
            span_id=new_span_id(),
            parent_span_id=context.span_id,
        )
        try:
            self._store.start_span(
                TraceSpanRecord(
                    span_id=span_context.span_id,
                    trace_id=span_context.trace_id,
                    run_id=span_context.run_id or "unbound",
                    task_id=span_context.task_id,
                    parent_span_id=span_context.parent_span_id,
                    name=name,
                    kind=kind,
                    status="running",
                    attributes=redact_value(attributes or {}),
                )
            )
        except Exception as exc:
            self._logger.warning(
                "trace_span_write_failed",
                extra={
                    "msg": f"Trace span 开始写入失败，span_name={name}",
                    "data": {
                        "task_id": context.task_id,
                        "span_name": name,
                        "operation": "start",
                        "error": str(exc),
                    },
                },
            )
        return span_context

    def finish_span(
        self,
        span_id: str,
        status: str,
        error: dict[str, Any] | None = None,
    ) -> None:
        """结束一个 span。

        参数:
            span_id: 待结束 span 标识。
            status: 结束状态。
            error: 可选错误摘要。

        返回:
            无。

        异常:
            无。写入失败会记录日志但不破坏主流程。

        副作用:
            尝试更新 TraceStore 中的 span。
        """

        if not span_id:
            return
        try:
            self._store.finish_span(
                span_id, status=status, error=redact_value(error or {}) if error else None
            )
        except Exception as exc:
            self._logger.warning(
                "trace_span_write_failed",
                extra={
                    "msg": f"Trace span 结束写入失败，span_id={span_id}，status={status}",
                    "data": {
                        "span_id": span_id,
                        "span_status": status,
                        "operation": "finish",
                        "error": str(exc),
                    },
                },
            )
