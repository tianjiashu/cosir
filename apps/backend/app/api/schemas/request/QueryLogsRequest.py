from pydantic import BaseModel, Field


class QueryLogsRequest(BaseModel):
    """校验按 trace 查询日志的请求参数。

    参数:
        trace_id: 必填 trace 标识（日志层唯一链路键）。
        level: 可选日志级别。
        start_time: 可选起始 UTC RFC3339 时间。
        end_time: 可选结束 UTC RFC3339 时间。
        limit: 最大返回数量（1-1000）。

    返回:
        Pydantic 请求模型（经 ``Depends()`` 作为查询参数注入）。

    异常:
        ValueError: 当 limit 越界或必填项缺失时由 FastAPI 抛出。

    副作用:
        无。
    """

    trace_id: str = Field(..., description="必填 trace 标识")
    level: str = Field(default="", description="可选日志级别")
    start_time: str = Field(default="", description="可选起始 UTC RFC3339 时间")
    end_time: str = Field(default="", description="可选结束 UTC RFC3339 时间")
    limit: int = Field(default=200, ge=1, le=1000, description="最大返回数量")
