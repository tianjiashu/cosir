"""日志查询 API 路由。

端点以模块级 ``@app.get`` 直接注册到 ``app.api.app.app`` 单例上，
运行时通过 ``Depends(get_runtime)`` 注入，不再由注册函数包裹。
"""
import logging

from fastapi import Depends, HTTPException, Query
from app.api.app import app
from app.api.dependencies import get_runtime
from app.core.runtime.runner import AgentRuntime
from app.service.log_query_service import LogQueryService
from app.storage.crud.log_crud import LogStore

_LOGGER = logging.getLogger("coding_agent.backend")


@app.get("/logs/query")
async def query_logs(
    trace_id: str,
    level: str = Query(default=""),
    start_time: str = Query(default=""),
    end_time: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    """按 trace_id 查询日志。

    参数:
        trace_id: 必填 trace 标识（日志层唯一链路键）。
        level: 可选日志级别。
        start_time: 可选起始 UTC RFC3339 时间。
        end_time: 可选结束 UTC RFC3339 时间。
        limit: 最大返回数量。

    返回:
        包含 entries 和 text 的日志查询响应。

    异常:
        HTTPException: 查询服务不可用、参数非法或底层查询失败时抛出。

    副作用:
        读取日志 SQLite。
    """

    try:

        query_service = LogQueryService(store=LogStore())
        return query_service.query_by_trace(
            trace_id=trace_id,
            level=level,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        ).to_dict()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="log query failed") from exc


@app.get("/logs/recent")
async def recent_logs(
    runtime: AgentRuntime = Depends(get_runtime),
    level: str = Query(default=""),
    start_time: str = Query(default=""),
    end_time: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    """查询最近日志。

    参数:
        tool_execute: 通过依赖注入的运行时单例。
        level: 可选日志级别。
        start_time: 可选起始 UTC RFC3339 时间。
        end_time: 可选结束 UTC RFC3339 时间。
        limit: 最大返回数量。

    返回:
        包含 entries 和 text 的日志查询响应。

    异常:
        HTTPException: 查询服务不可用、参数非法或底层查询失败时抛出。

    副作用:
        读取日志 SQLite。
    """

    try:
        query_service = LogQueryService(store=LogStore())
        return query_service.recent(
            level=level,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        ).to_dict()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="log query failed") from exc
