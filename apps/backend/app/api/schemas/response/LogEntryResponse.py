from typing import Any

from pydantic import BaseModel


class LogEntryResponse(BaseModel):
    """校验并序列化单条结构化日志（与 ``LogEntryRecord.to_dict()`` 对齐）。

    参数:
        ts: UTC RFC3339 日志时间。
        level: 日志级别。
        logger: logger 名称。
        trace_id: 链路关联键。
        caller: 调用位置。
        event: 稳定事件名。
        msg: 中文可读消息。
        data: 结构化业务字段。
        error: 嵌套错误块，正常为 None。
        truncated: 是否发生截断。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    ts: str
    level: str
    logger: str
    trace_id: str
    caller: str
    event: str
    msg: str
    data: dict[str, Any] = {}
    error: dict[str, str] | None = None
    truncated: bool = False
