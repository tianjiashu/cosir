from pydantic import BaseModel

from app.api.schemas.response.LogEntryResponse import LogEntryResponse


class LogQueryResponse(BaseModel):
    """校验并序列化日志查询结果响应（与 ``LogQueryResult.to_dict()`` 对齐）。

    参数:
        entries: 结构化日志记录列表。
        text: 可直接展示的纯文本日志。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    entries: list[LogEntryResponse]
    text: str
