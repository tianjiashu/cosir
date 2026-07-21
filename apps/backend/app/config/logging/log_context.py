"""日志上下文自动注入工具。

按《通用日志开发规范》第六章，日志层**只保留 ``trace_id`` 一个链路关联键**：
入口层绑定 ``trace_id``，下层继承，由 :class:`LogContextFilter` 自动回填到
LogRecord。业务实体 ID（``run_id`` / ``task_id`` …）不进日志顶层字段，需要时
放入 ``data``。

为支持"只知道 run_id / task_id 也能定位 trace"的入口层绑定（如恢复、审批），
本模块维护一张进程内 ``run_id/task_id -> trace_id`` 反查表：full trace 上下文
绑定时登记，后续 ``merge_log_context(run_id=...)`` 可据此解析出 ``trace_id``。
该反查表仅用于入口层解析 ``trace_id``，不再写入任何日志字段。
"""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from app.models import TraceContext


@dataclass(frozen=True)
class LogContext:
    """当前执行链路的日志关联字段。

    参数:
        trace_id: 一次前端用户操作触发的完整链路标识，是日志层唯一链路键。

    返回:
        不可变日志上下文对象。

    异常:
        无。

    副作用:
        无。
    """

    trace_id: str = ""

    def to_extra(self) -> dict[str, str]:
        """转换为 logging extra 字段。

        参数:
            无。

        返回:
            非空时包含 ``trace_id`` 的字典，否则为空字典。

        异常:
            无。

        副作用:
            无。
        """

        return {"trace_id": self.trace_id} if self.trace_id else {}


_CURRENT_LOG_CONTEXT: ContextVar[LogContext] = ContextVar(
    "coding_agent_log_context",
    default=LogContext(),
)
# 进程内反查表：业务实体 ID -> trace_id，仅供入口层解析 trace_id，不写日志。
_TRACE_BY_RUN: dict[str, str] = {}
_TRACE_BY_TASK: dict[str, str] = {}


class LogContextFilter:
    """为 LogRecord 自动回填当前链路 ``trace_id``。"""

    def filter(self, record) -> bool:
        """向缺失 ``trace_id`` 的日志记录回填当前链路标识。

        参数:
            record: Python logging 传入的 LogRecord。

        返回:
            始终返回 True，表示不过滤日志。

        异常:
            无。

        副作用:
            当记录缺失 ``trace_id`` 时在其上补齐。
        """

        trace_id = current_log_context().trace_id
        if trace_id and not getattr(record, "trace_id", ""):
            record.trace_id = trace_id
        return True


TraceLogContextFilter = LogContextFilter


def current_log_context() -> LogContext:
    """返回当前执行上下文中的日志上下文。

    参数:
        无。

    返回:
        当前 LogContext；没有绑定时返回空上下文。

    异常:
        无。

    副作用:
        无。
    """

    return _CURRENT_LOG_CONTEXT.get()


def set_log_context(context: LogContext | TraceContext) -> Token:
    """设置当前执行上下文的链路 ``trace_id``。

    参数:
        context: 新的 LogContext；为兼容入口层调用，也可传入 TraceContext
            （会登记其 run_id/task_id -> trace_id 反查关系）。

    返回:
        可传给 ``reset_log_context`` 的 token。

    异常:
        TypeError: 如果传入对象不是 LogContext 或 TraceContext。

    副作用:
        修改当前 contextvars 上下文，并登记 run/task -> trace 反查映射。
    """

    trace_id, run_id, task_id = _extract_ids(context)
    _register_trace(trace_id, run_id, task_id)
    resolved = trace_id or _resolve_trace(run_id, task_id)
    return _CURRENT_LOG_CONTEXT.set(LogContext(trace_id=resolved))


def merge_log_context(**fields: str | None) -> Token:
    """在当前链路上下文基础上解析并绑定 ``trace_id``。

    仅识别 ``trace_id`` / ``run_id`` / ``task_id`` 三个键：``trace_id`` 直接使用；
    否则用 ``run_id`` / ``task_id`` 经反查表解析 ``trace_id``；再否则沿用当前
    上下文的 ``trace_id``。其余键（如 ``tool_call_id``）不再作为
    链路键，忽略即可（其值应由调用方写入日志 ``data``）。

    参数:
        fields: 可包含 ``trace_id`` / ``run_id`` / ``task_id`` 的入口层字段。

    返回:
        可传给 ``reset_log_context`` 的 token。

    异常:
        无。

    副作用:
        修改当前 contextvars 上下文，并登记 run/task -> trace 反查映射。
    """

    trace_id = str(fields.get("trace_id") or "")
    run_id = str(fields.get("run_id") or "")
    task_id = str(fields.get("task_id") or "")
    _register_trace(trace_id, run_id, task_id)
    resolved = trace_id or _resolve_trace(run_id, task_id) or current_log_context().trace_id
    return _CURRENT_LOG_CONTEXT.set(LogContext(trace_id=resolved))


def bind_log_context(context: LogContext | TraceContext) -> None:
    """登记 run/task 与 trace_id 的反查关系。

    参数:
        context: 需要登记的 LogContext 或 TraceContext。

    返回:
        无。

    异常:
        TypeError: 如果传入对象不是 LogContext 或 TraceContext。

    副作用:
        更新进程内 run/task -> trace 反查映射。
    """

    trace_id, run_id, task_id = _extract_ids(context)
    _register_trace(trace_id, run_id, task_id)


def reset_log_context(token: Token) -> None:
    """恢复先前的链路上下文。

    参数:
        token: ``set_log_context`` 或 ``merge_log_context`` 返回的 token。

    返回:
        无。

    异常:
        ValueError: 如果 token 不属于当前 ContextVar。

    副作用:
        修改当前 contextvars 上下文。
    """

    _CURRENT_LOG_CONTEXT.reset(token)


def trace_log_extra(context: TraceContext | LogContext, **fields: Any) -> dict[str, Any]:
    """根据上下文构造仅含 ``trace_id`` 的 logging extra 字段。

    参数:
        context: 当前 trace 或日志上下文。
        fields: 额外需要写入 LogRecord 的字段（如 ``msg`` / ``data``）。

    返回:
        含 ``trace_id``（非空时）以及额外字段的字典。

    异常:
        无。

    副作用:
        无。
    """

    trace_id = _extract_ids(context)[0]
    extra: dict[str, Any] = {"trace_id": trace_id} if trace_id else {}
    extra.update(fields)
    return extra


def _extract_ids(context: LogContext | TraceContext) -> tuple[str, str, str]:
    """从上下文对象提取 ``(trace_id, run_id, task_id)``。

    参数:
        context: LogContext 或 TraceContext。

    返回:
        ``(trace_id, run_id, task_id)`` 三元组，缺失字段为空串。

    异常:
        TypeError: 如果对象类型不受支持。

    副作用:
        无。
    """

    if isinstance(context, LogContext):
        return context.trace_id, "", ""
    if isinstance(context, TraceContext):
        return context.trace_id, context.run_id, context.task_id
    raise TypeError("context must be LogContext or TraceContext")


def _register_trace(trace_id: str, run_id: str, task_id: str) -> None:
    """登记 run/task 到 trace_id 的反查关系。

    参数:
        trace_id: 链路标识；为空时不登记。
        run_id: Durable Run 标识；非空时登记。
        task_id: 任务标识；非空时登记。

    返回:
        无。

    异常:
        无。

    副作用:
        更新进程内反查映射。
    """

    if not trace_id:
        return
    if run_id:
        _TRACE_BY_RUN[run_id] = trace_id
    if task_id:
        _TRACE_BY_TASK[task_id] = trace_id


def _resolve_trace(run_id: str, task_id: str) -> str:
    """按 run_id / task_id 从反查表解析 trace_id。

    参数:
        run_id: Durable Run 标识。
        task_id: 任务标识。

    返回:
        命中反查表时返回 trace_id，否则返回空串。

    异常:
        无。

    副作用:
        无。
    """

    if run_id and run_id in _TRACE_BY_RUN:
        return _TRACE_BY_RUN[run_id]
    if task_id and task_id in _TRACE_BY_TASK:
        return _TRACE_BY_TASK[task_id]
    return ""
