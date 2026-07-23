"""Trace 查询 API 路由。

端点以模块级 ``@app.get`` 直接注册到 ``app.api.app.app`` 单例上，
运行时通过 ``Depends(get_trace_query_service)`` 注入，不再由注册函数包裹。
"""

from datetime import date
from typing import Any

from fastapi import Depends, HTTPException, Query

from app.api.app import app
from app.api.depends.dependencies import get_trace_query_service
from app.api.schemas import (
    ListTraceLogsRequest,
    RunTraceResponse,
    TraceDetailResponse,
    TraceEventResponse,
    TraceSpanResponse,
    TraceSummaryResponse,
)
from app.service.trace.trace_query_service import TraceQueryService


@app.get("/traces")
async def list_traces(
    trace_service: TraceQueryService = Depends(get_trace_query_service),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[TraceSummaryResponse]:
    """返回 trace 摘要列表。

    参数:
        trace_service: 通过依赖注入的 trace 查询服务。
        limit: 最大返回数量。

    返回:
        ``TraceSummaryResponse`` 列表。

    异常:
        HTTPException: Trace 服务不可用或查询参数非法时抛出。

    副作用:
        无。
    """

    try:
        return trace_service.list_traces(limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    trace_service: TraceQueryService = Depends(get_trace_query_service),
) -> TraceDetailResponse:
    """返回 trace 摘要与详情。

    参数:
        trace_id: 路由中的 trace 标识。
        trace_service: 通过依赖注入的 trace 查询服务。

    返回:
        ``TraceDetailResponse``：含 trace summary、events、spans 和 logs。

    异常:
        HTTPException: Trace 服务不可用或 trace 不存在时抛出。

    副作用:
        读取 SQLite 与 JSONL 日志文件。
    """

    try:
        return TraceDetailResponse(**trace_service.get_trace_summary(trace_id))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="trace not found") from exc


@app.get("/traces/{trace_id}/events")
async def list_trace_events(
    trace_id: str,
    trace_service: TraceQueryService = Depends(get_trace_query_service),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[TraceEventResponse]:
    """返回指定 trace 的 ledger events。

    参数:
        trace_id: 路由中的 trace 标识。
        trace_service: 通过依赖注入的 trace 查询服务。
        limit: 最大返回数量。

    返回:
        ``TraceEventResponse`` 列表。

    异常:
        HTTPException: Trace 服务不可用或查询参数非法时抛出。

    副作用:
        无。
    """

    try:
        return trace_service.list_events(trace_id=trace_id, limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/traces/{trace_id}/spans")
async def list_trace_spans(
    trace_id: str,
    trace_service: TraceQueryService = Depends(get_trace_query_service),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[TraceSpanResponse]:
    """返回指定 trace 的 spans。

    参数:
        trace_id: 路由中的 trace 标识。
        trace_service: 通过依赖注入的 trace 查询服务。
        limit: 最大返回数量。

    返回:
        ``TraceSpanResponse`` 列表。

    异常:
        HTTPException: Trace 服务不可用或查询参数非法时抛出。

    副作用:
        无。
    """

    try:
        return trace_service.list_spans(trace_id=trace_id, limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/traces/{trace_id}/logs")
async def list_trace_logs(
    trace_id: str,
    trace_service: TraceQueryService = Depends(get_trace_query_service),
    req: ListTraceLogsRequest = Depends(),
) -> list[dict[str, Any]]:
    """从 JSONL 文件返回指定 trace 的日志。

    参数:
        trace_id: 路由中的 trace 标识。
        trace_service: 通过依赖注入的 trace 查询服务。
        req: 经依赖注入的查询参数（``date`` / ``level`` / 时间区间 / ``limit``）。

    返回:
        JSONL 日志行列表（动态结构，保留为 ``dict``）。

    异常:
        HTTPException: Trace 服务不可用或查询参数非法时抛出。

    副作用:
        读取日志文件。
    """

    try:
        log_date = _parse_log_date(req.date)
        return trace_service.list_logs(
            trace_id=trace_id,
            log_date=log_date,
            level=req.level,
            start_time=req.start_time,
            end_time=req.end_time,
            limit=req.limit,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/runs/{run_id}/trace")
async def get_run_trace(
    run_id: str,
    trace_service: TraceQueryService = Depends(get_trace_query_service),
) -> RunTraceResponse:
    """返回 run 的 trace 摘要。

    参数:
        run_id: 路由中的 run 标识。
        trace_service: 通过依赖注入的 trace 查询服务。

    返回:
        ``RunTraceResponse``：含 events、spans、logs 的摘要。

    异常:
        HTTPException: Trace 服务不可用或 run_id 非法时抛出。

    副作用:
        读取 SQLite 与 JSONL 日志文件。
    """

    try:
        return RunTraceResponse(**trace_service.get_run_trace(run_id))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _parse_log_date(value: str) -> date | None:
    """解析日志查询日期参数。

    参数:
        value: API 查询参数中的日期文本，允许为空。

    返回:
        日期对象；空字符串返回 None。

    异常:
        ValueError: 如果非空文本不是 YYYY-MM-DD 日期。

    副作用:
        无。
    """

    if not value:
        return None
    return date.fromisoformat(value)
