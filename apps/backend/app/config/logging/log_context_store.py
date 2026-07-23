"""日志上下文状态存储与入口层绑定。

按《通用日志开发规范》第六章，日志层**只保留 ``trace_id`` 一个链路关联键**：
入口层绑定 ``trace_id``，下层继承，由 :class:`LogContextFilter` 自动回填到
LogRecord。业务实体 ID（``run_id`` / ``task_id`` …）不进日志顶层字段，需要时
放入 ``data``。

为支持"只知道 run_id / task_id 也能定位 trace"的入口层绑定（如恢复、审批），
本模块维护一张进程内 ``run_id/task_id -> trace_id`` 反查表：full trace 上下文
绑定时登记，后续 :meth:`LogContextStore.merge` 可据此解析出 ``trace_id``。
该反查表仅用于入口层解析 ``trace_id``，不再写入任何日志字段。

:class:`LogContextStore` 封装上下文的读取、绑定、合并、登记与恢复，并以模块级
单例 :data:`_STORE` 暴露函数式 API，兼容 ``app.config.logging`` 的既有导出。
"""

from contextvars import ContextVar, Token
from typing import Any

from app.config.logging.log_context import LogContext
from app.models import TraceContext


class LogContextStore:
    """进程内日志上下文存储与链路键解析。

    职责：
    - 通过 ``ContextVar`` 维护当前执行链路的 ``LogContext``；
    - 维护 ``run_id`` / ``task_id`` 到 ``trace_id`` 的反查表，仅用于入口层解析；
    - 提供绑定、合并、登记、恢复与 extra 构造等入口层操作。
    """

    def __init__(self) -> None:
        """初始化上下文变量与反查表。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建进程内的 ``ContextVar`` 与两张反查映射。
        """

        self._current: ContextVar[LogContext | None] = ContextVar(
            "coding_agent_log_context",
            default=None,
        )
        # 进程内反查表：业务实体 ID -> trace_id，仅供入口层解析 trace_id，不写日志。
        self._trace_by_run: dict[str, str] = {}
        self._trace_by_task: dict[str, str] = {}

    def current(self) -> LogContext:
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

        return self._current.get() or LogContext()

    def set(self, context: LogContext | TraceContext) -> Token:
        """设置当前执行上下文的链路 ``trace_id``。

        参数:
            context: 新的 LogContext；为兼容入口层调用，也可传入 TraceContext
                （会登记其 run_id/task_id -> trace_id 反查关系）。

        返回:
            可传给 :meth:`reset` 的 token。

        异常:
            TypeError: 如果传入对象不是 LogContext 或 TraceContext。

        副作用:
            修改当前 ContextVar 上下文，并登记 run/task -> trace 反查映射。
        """

        trace_id, run_id, task_id = self._extract_ids(context)
        self._register_trace(trace_id, run_id, task_id)
        resolved = trace_id or self._resolve_trace(run_id, task_id)
        return self._current.set(LogContext(trace_id=resolved))

    def merge(self, **fields: str | None) -> Token:
        """在当前链路上下文基础上解析并绑定 ``trace_id``。

        仅识别 ``trace_id`` / ``run_id`` / ``task_id`` 三个键：``trace_id`` 直接使用；
        否则用 ``run_id`` / ``task_id`` 经反查表解析 ``trace_id``；再否则沿用当前
        上下文的 ``trace_id``。其余键（如 ``tool_call_id``）不再作为
        链路键，忽略即可（其值应由调用方写入日志 ``data``）。

        参数:
            fields: 可包含 ``trace_id`` / ``run_id`` / ``task_id`` 的入口层字段。

        返回:
            可传给 :meth:`reset` 的 token。

        异常:
            无。

        副作用:
            修改当前 ContextVar 上下文，并登记 run/task -> trace 反查映射。
        """

        trace_id = str(fields.get("trace_id") or "")
        run_id = str(fields.get("run_id") or "")
        task_id = str(fields.get("task_id") or "")
        self._register_trace(trace_id, run_id, task_id)
        resolved = trace_id or self._resolve_trace(run_id, task_id) or self.current().trace_id
        return self._current.set(LogContext(trace_id=resolved))

    def bind(self, context: LogContext | TraceContext) -> None:
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

        trace_id, run_id, task_id = self._extract_ids(context)
        self._register_trace(trace_id, run_id, task_id)

    def reset(self, token: Token) -> None:
        """恢复先前的链路上下文。

        参数:
            token: :meth:`set` 或 :meth:`merge` 返回的 token。

        返回:
            无。

        异常:
            ValueError: 如果 token 不属于当前 ContextVar。

        副作用:
            修改当前 ContextVar 上下文。
        """

        self._current.reset(token)

    def extra_for(self, context: TraceContext | LogContext, **fields: Any) -> dict[str, Any]:
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

        trace_id = self._extract_ids(context)[0]
        extra: dict[str, Any] = {"trace_id": trace_id} if trace_id else {}
        extra.update(fields)
        return extra

    def _extract_ids(self, context: LogContext | TraceContext) -> tuple[str, str, str]:
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

    def _register_trace(self, trace_id: str, run_id: str, task_id: str) -> None:
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
            self._trace_by_run[run_id] = trace_id
        if task_id:
            self._trace_by_task[task_id] = trace_id

    def _resolve_trace(self, run_id: str, task_id: str) -> str:
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

        if run_id and run_id in self._trace_by_run:
            return self._trace_by_run[run_id]
        if task_id and task_id in self._trace_by_task:
            return self._trace_by_task[task_id]
        return ""


# 进程内唯一存储实例；模块级函数式 API 均委托给它，以保持对外兼容。
_STORE = LogContextStore()


def current_log_context() -> LogContext:
    """返回当前执行上下文中的日志上下文（委托 :class:`LogContextStore`）。

    参数:
        无。

    返回:
        当前 LogContext；没有绑定时返回空上下文。

    异常:
        无。

    副作用:
        无。
    """

    return _STORE.current()


def set_log_context(context: LogContext | TraceContext) -> Token:
    """设置当前执行上下文的链路 ``trace_id``（委托 :class:`LogContextStore`）。

    参数:
        context: 新的 LogContext，或兼容入口层调用的 TraceContext。

    返回:
        可传给 :func:`reset_log_context` 的 token。

    异常:
        TypeError: 如果传入对象不是 LogContext 或 TraceContext。

    副作用:
        修改当前 ContextVar 上下文，并登记 run/task -> trace 反查映射。
    """

    return _STORE.set(context)


def merge_log_context(**fields: str | None) -> Token:
    """在当前链路上下文基础上解析并绑定 ``trace_id``（委托 :class:`LogContextStore`）。

    参数:
        fields: 可包含 ``trace_id`` / ``run_id`` / ``task_id`` 的入口层字段。

    返回:
        可传给 :func:`reset_log_context` 的 token。

    异常:
        无。

    副作用:
        修改当前 ContextVar 上下文，并登记 run/task -> trace 反查映射。
    """

    return _STORE.merge(**fields)


def bind_log_context(context: LogContext | TraceContext) -> None:
    """登记 run/task 与 trace_id 的反查关系（委托 :class:`LogContextStore`）。

    参数:
        context: 需要登记的 LogContext 或 TraceContext。

    返回:
        无。

    异常:
        TypeError: 如果传入对象不是 LogContext 或 TraceContext。

    副作用:
        更新进程内 run/task -> trace 反查映射。
    """

    _STORE.bind(context)


def reset_log_context(token: Token) -> None:
    """恢复先前的链路上下文（委托 :class:`LogContextStore`）。

    参数:
        token: :func:`set_log_context` 或 :func:`merge_log_context` 返回的 token。

    返回:
        无。

    异常:
        ValueError: 如果 token 不属于当前 ContextVar。

    副作用:
        修改当前 ContextVar 上下文。
    """

    _STORE.reset(token)


def trace_log_extra(context: TraceContext | LogContext, **fields: Any) -> dict[str, Any]:
    """根据上下文构造仅含 ``trace_id`` 的 logging extra 字段（委托 :class:`LogContextStore`）。

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

    return _STORE.extra_for(context, **fields)
