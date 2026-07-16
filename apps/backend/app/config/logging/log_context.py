"""日志上下文自动注入工具。"""

from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any

from app.core.trace.context import TraceContext


@dataclass(frozen=True)
class LogContext:
    """当前执行链路的日志关联字段。

    参数:
        trace_id: 一次前端用户操作触发的完整链路标识。
        run_id: Durable Run 标识。
        task_id: 任务标识。
        span_id: Trace span 标识。
        tool_call_id: 工具调用标识。
        approval_id: 审批请求标识。

    返回:
        不可变日志上下文对象。

    异常:
        无。

    副作用:
        无。
    """

    trace_id: str = ""
    run_id: str = ""
    task_id: str = ""
    span_id: str = ""
    tool_call_id: str = ""
    approval_id: str = ""

    def to_extra(self) -> dict[str, str]:
        """转换为 logging extra 字段。

        参数:
            无。

        返回:
            包含非空日志上下文字段的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {key: value for key, value in self.__dict__.items() if value}


_CURRENT_LOG_CONTEXT: ContextVar[LogContext] = ContextVar(
    "coding_agent_log_context",
    default=LogContext(),
)
_TASK_CONTEXTS: dict[str, LogContext] = {}
_RUN_CONTEXTS: dict[str, LogContext] = {}


class LogContextFilter:
    """为 LogRecord 自动注入当前日志上下文字段。"""

    def filter(self, record) -> bool:
        """向日志记录补齐 trace、run、task 等字段。

        参数:
            record: Python logging 传入的 LogRecord。

        返回:
            始终返回 True，表示不过滤日志。

        异常:
            无。

        副作用:
            修改 LogRecord 上缺失的关联字段。
        """

        context = _enrich_from_registry(current_log_context())
        record_context = _lookup_record_context(record)
        if record_context is not None:
            context = _merge_missing_context(context, record_context)
        for key, value in context.to_extra().items():
            if not getattr(record, key, ""):
                setattr(record, key, value)
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
    """设置当前执行上下文的日志关联字段。

    参数:
        context: 新的 LogContext；为兼容旧调用，也可传入 TraceContext。

    返回:
        可传给 ``reset_log_context`` 的 token。

    异常:
        TypeError: 如果传入对象不是 LogContext 或 TraceContext。

    副作用:
        修改当前 contextvars 上下文，并登记 task/run 反查映射。
    """

    log_context = _coerce_log_context(context)
    bind_log_context(log_context)
    return _CURRENT_LOG_CONTEXT.set(log_context)


def merge_log_context(**fields: str | None) -> Token:
    """在当前日志上下文基础上合并非空字段。

    参数:
        fields: 需要覆盖或补充的日志上下文字段。

    返回:
        可传给 ``reset_log_context`` 的 token。

    异常:
        TypeError: 如果字段名不是 LogContext 支持的字段。

    副作用:
        修改当前 contextvars 上下文，并登记 task/run 反查映射。
    """

    clean_fields = {key: str(value) for key, value in fields.items() if value is not None and str(value)}
    context = _enrich_from_registry(replace(current_log_context(), **clean_fields))
    bind_log_context(context)
    return _CURRENT_LOG_CONTEXT.set(context)


def bind_log_context(context: LogContext | TraceContext) -> None:
    """登记 task/run 与日志上下文的关联关系。

    参数:
        context: 需要登记的 LogContext 或 TraceContext。

    返回:
        无。

    异常:
        TypeError: 如果传入对象不是 LogContext 或 TraceContext。

    副作用:
        更新进程内日志关联映射。
    """

    log_context = _coerce_log_context(context)
    durable_context = _durable_log_context(log_context)
    if durable_context.task_id:
        _TASK_CONTEXTS[durable_context.task_id] = durable_context
    if durable_context.run_id:
        _RUN_CONTEXTS[durable_context.run_id] = durable_context


def reset_log_context(token: Token) -> None:
    """恢复先前的日志关联上下文。

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


def trace_log_extra(context: TraceContext, **fields: Any) -> dict[str, Any]:
    """根据 TraceContext 构造 logging extra 字段。

    参数:
        context: 当前 trace 上下文。
        fields: 额外需要写入 LogRecord 的字段。

    返回:
        包含 trace_id、run_id、task_id、span_id 以及额外字段的字典。

    异常:
        无。

    副作用:
        无。
    """

    extra = _coerce_log_context(context).to_extra()
    extra.update(fields)
    return extra


def _coerce_log_context(context: LogContext | TraceContext) -> LogContext:
    """将支持的上下文对象转换为 LogContext。

    参数:
        context: LogContext 或 TraceContext。

    返回:
        LogContext 实例。

    异常:
        TypeError: 如果对象类型不受支持。

    副作用:
        无。
    """

    if isinstance(context, LogContext):
        return context
    if isinstance(context, TraceContext):
        current = current_log_context()
        return replace(
            current,
            trace_id=context.trace_id,
            run_id=context.run_id,
            task_id=context.task_id,
            span_id=context.span_id,
        )
    raise TypeError("context must be LogContext or TraceContext")


def _durable_log_context(context: LogContext) -> LogContext:
    """返回可跨请求缓存的 durable 日志上下文。

    参数:
        context: 当前完整日志上下文。

    返回:
        去掉局部业务字段后的 LogContext。

    异常:
        无。

    副作用:
        无。
    """

    return LogContext(
        trace_id=context.trace_id,
        run_id=context.run_id,
        task_id=context.task_id,
        span_id=context.span_id,
    )


def _enrich_from_registry(context: LogContext) -> LogContext:
    """根据 run_id 或 task_id 回填 durable trace 上下文。

    参数:
        context: 当前日志上下文。

    返回:
        已尽量补齐 trace_id、run_id、task_id、span_id 的上下文。

    异常:
        无。

    副作用:
        无。
    """

    durable_context = None
    if context.run_id:
        durable_context = _RUN_CONTEXTS.get(context.run_id)
    if durable_context is None and context.task_id:
        durable_context = _TASK_CONTEXTS.get(context.task_id)
    if durable_context is None:
        return context
    return _merge_missing_context(context, durable_context)


def _merge_missing_context(primary: LogContext, fallback: LogContext) -> LogContext:
    """用 fallback 补齐 primary 中为空的字段。

    参数:
        primary: 优先保留的日志上下文。
        fallback: 用于补齐空字段的上下文。

    返回:
        合并后的 LogContext。

    异常:
        无。

    副作用:
        无。
    """

    merged = primary
    for key, value in fallback.to_extra().items():
        if not getattr(merged, key):
            merged = replace(merged, **{key: value})
    return merged


def _lookup_record_context(record) -> LogContext | None:
    """根据 LogRecord 结构化字段反查 LogContext。

    参数:
        record: Python logging 记录。

    返回:
        找到时返回 LogContext，否则返回 None。

    异常:
        无。

    副作用:
        无。
    """

    run_id = str(getattr(record, "run_id", "") or "")
    task_id = str(getattr(record, "task_id", "") or "")
    if run_id and run_id in _RUN_CONTEXTS:
        return _RUN_CONTEXTS[run_id]
    if task_id and task_id in _TASK_CONTEXTS:
        return _TASK_CONTEXTS[task_id]
    return None
