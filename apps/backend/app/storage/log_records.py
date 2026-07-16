"""日志查询与持久化记录结构。"""

from dataclasses import dataclass, field
from typing import Any, Literal


LogSortOrder = Literal["asc", "desc"]


@dataclass(frozen=True)
class LogEntryRecord:
    """表示 SQLite 中的一条结构化日志。

    参数:
        ts: UTC RFC3339 日志时间。
        level: Python 标准日志级别。
        logger_name: logger 名称。
        event_name: 稳定事件名。
        message: 人类可读展示文本。
        attributes: 可变业务字段。
        trace_id: 前端操作 trace 标识。
        task_id: 任务标识。
        run_id: Durable Run 标识。
        span_id: Trace span 标识。
        event_id: 事件标识。
        step_id: 步骤标识。
        tool_call_id: 工具调用标识。
        approval_id: 审批标识。
        error_type: 异常类型。
        error_message: 异常消息。
        stack: 异常栈。
        truncated: 是否发生截断。

    返回:
        不可变日志记录。

    异常:
        无。

    副作用:
        无。
    """

    ts: str
    level: str
    logger_name: str
    event_name: str
    message: str
    attributes: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    task_id: str = ""
    run_id: str = ""
    span_id: str = ""
    event_id: str = ""
    step_id: str = ""
    tool_call_id: str = ""
    approval_id: str = ""
    error_type: str = ""
    error_message: str = ""
    stack: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 可返回的字典。

        参数:
            无。

        返回:
            包含日志字段和 attributes 的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "ts": self.ts,
            "level": self.level,
            "logger_name": self.logger_name,
            "event_name": self.event_name,
            "message": self.message,
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "span_id": self.span_id,
            "event_id": self.event_id,
            "step_id": self.step_id,
            "tool_call_id": self.tool_call_id,
            "approval_id": self.approval_id,
            "attributes": self.attributes,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "stack": self.stack,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class LogQuery:
    """表示日志查询参数。

    参数:
        trace_id: 可选 trace 过滤条件。
        level: 可选日志级别过滤条件。
        event_name: 可选事件名过滤条件。
        task_id: 可选任务过滤条件。
        run_id: 可选 run 过滤条件。
        start_time: 可选 UTC RFC3339 起始时间。
        end_time: 可选 UTC RFC3339 结束时间。
        limit: 最大返回数量。
        order: 返回排序方向。

    返回:
        不可变查询参数。

    异常:
        无。

    副作用:
        无。
    """

    trace_id: str = ""
    level: str = ""
    event_name: str = ""
    task_id: str = ""
    run_id: str = ""
    start_time: str = ""
    end_time: str = ""
    limit: int = 200
    order: LogSortOrder = "asc"


@dataclass(frozen=True)
class LogQueryResult:
    """表示日志查询结果。

    参数:
        entries: 结构化日志记录列表。
        text: 可直接展示的纯文本日志。

    返回:
        不可变查询结果。

    异常:
        无。

    副作用:
        无。
    """

    entries: list[LogEntryRecord]
    text: str

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 响应字典。

        参数:
            无。

        返回:
            包含 entries 与 text 的响应字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "text": self.text,
        }
