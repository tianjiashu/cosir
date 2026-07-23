"""日志查询 API 路由。

端点以模块级 ``@app.get`` 直接注册到 ``app.api.app.app`` 单例上，
运行时通过 ``Depends(get_runtime)`` 注入，不再由注册函数包裹。
"""
import logging

from fastapi import Depends, HTTPException
from pydantic import ValidationError

from app.api.app import app
from app.api.schemas import LogQueryResponse, QueryLogsRequest, RecentLogsRequest
from app.service.log_query_service import LogQueryService
from app.storage.crud.log_crud import LogStore

_LOGGER = logging.getLogger("coding_agent.backend")


@app.get("/logs/query")
async def query_logs(
    req: QueryLogsRequest = Depends(),
) -> LogQueryResponse:
    """按 trace_id 查询日志。

    参数:
        req: 经依赖注入的查询参数（``trace_id`` / ``level`` / 时间区间 / ``limit``）。

    返回:
        ``LogQueryResponse``：包含 entries 和 text 的日志查询结果。
        前端直接使用text渲染即可

    异常:
        HTTPException: 查询服务不可用、参数非法或底层查询失败时抛出。

    副作用:
        读取日志 SQLite。
    """

    try:
        query_service = LogQueryService(store=LogStore())
        result = query_service.query_by_trace(
            trace_id=req.trace_id,
            level=req.level,
            start_time=req.start_time,
            end_time=req.end_time,
            limit=req.limit,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="log query failed") from exc

    try:
        return LogQueryResponse(**result.to_dict())
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=f"response schema mismatch: {exc}"
        ) from exc


@app.get("/logs/recent")
async def recent_logs(
    req: RecentLogsRequest = Depends(),
) -> LogQueryResponse:
    """查询最近日志。

    参数:
        req: 经依赖注入的查询参数（``level`` / 时间区间 / ``limit``）。

    返回:
        ``LogQueryResponse``：包含 entries 和 text 的日志查询结果。

    异常:
        HTTPException: 查询服务不可用、参数非法或底层查询失败时抛出。

    副作用:
        读取日志 SQLite。
    """

    try:
        query_service = LogQueryService(store=LogStore())
        result = query_service.recent(
            level=req.level,
            start_time=req.start_time,
            end_time=req.end_time,
            limit=req.limit,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="log query failed") from exc

    try:
        return LogQueryResponse(**result.to_dict())
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=f"response schema mismatch: {exc}"
        ) from exc
