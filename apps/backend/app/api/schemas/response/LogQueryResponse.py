from pydantic import BaseModel, Field

from app.api.schemas.response.LogEntryResponse import LogEntryResponse


class LogQueryResponse(BaseModel):
    """校验并序列化日志查询结果响应（与 ``LogQueryResult.to_dict()`` 对齐）。

    参数:
        entries: 结构化日志记录列表。
        text: 可直接展示的纯文本日志。
        total: 满足过滤条件的总记录数（不受 limit/offset 影响）。
        has_more: 当前页之后是否还有更多记录。
        level_counts: 各级别日志计数（忽略 level 过滤、含其余过滤条件），供前端展示级别分布。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    entries: list[LogEntryResponse]
    text: str
    total: int = 0
    has_more: bool = False
    level_counts: dict[str, int] = Field(default_factory=dict)
