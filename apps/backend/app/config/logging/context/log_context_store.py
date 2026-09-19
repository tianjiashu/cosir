"""进程内日志 trace_id 上下文。

只负责通过 ``ContextVar`` 保存当前异步任务/线程的 ``trace_id``，不维护业务实体反查表，
也不负责生成 trace_id。HTTP middleware 负责生成或接收入口 trace_id，并在请求结束时恢复
之前的上下文。
"""

from contextvars import ContextVar, Token

_CURRENT_TRACE_ID: ContextVar[str] = ContextVar("coding_agent_log_context", default="")


def current_log_context() -> str:
    """返回当前上下文中的 trace_id；未绑定时返回空字符串。"""

    return _CURRENT_TRACE_ID.get()


def merge_log_context(**fields: str | None) -> Token[str]:
    """绑定入口提供的 trace_id，并返回可用于恢复的 ContextVar token。

    只消费 ``trace_id`` 字段；业务实体 ID 应由调用方放入日志 ``data``，不在日志层维护
    run/task 到 trace 的全局映射。
    """

    trace_id = str(fields.get("trace_id") or "").strip() or current_log_context()
    return _CURRENT_TRACE_ID.set(trace_id)


def reset_log_context(token: Token[str]) -> None:
    """恢复 ``merge_log_context`` 之前的上下文。"""

    _CURRENT_TRACE_ID.reset(token)
