from pydantic import BaseModel, Field


class ListTraceLogsRequest(BaseModel):
    """校验按 trace 查询 JSONL 日志的请求参数。

    参数:
        date: 可选日志日期，格式 YYYY-MM-DD。
        level: 可选日志级别过滤条件。
        start_time: 可选起始 ISO 时间。
        end_time: 可选结束 ISO 时间。
        limit: 最大返回数量（1-1000）。

    返回:
        Pydantic 请求模型（经 ``Depends()`` 作为查询参数注入）。

    异常:
        ValueError: 当 limit 越界时由 FastAPI 抛出。

    副作用:
        无。
    """

    date: str = Field(default="", description="可选日志日期 YYYY-MM-DD")
    level: str = Field(default="", description="可选日志级别")
    start_time: str = Field(default="", description="可选起始 ISO 时间")
    end_time: str = Field(default="", description="可选结束 ISO 时间")
    limit: int = Field(default=200, ge=1, le=1000, description="最大返回数量")
