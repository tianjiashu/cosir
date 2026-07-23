"""日志记录自动回填链路标识的过滤器。

参考 logging 模块的 ``Filter`` 机制，在记录缺失 ``trace_id`` 时，从当前执行
上下文（见 :mod:`app.config.logging.log_context_store`）取出 ``trace_id``
并回填到 :class:`logging.LogRecord`。
"""

from app.config.logging.context.log_context_store import current_log_context


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

        trace_id = current_log_context()
        if trace_id and not getattr(record, "trace_id", ""):
            record.trace_id = trace_id
        return True

