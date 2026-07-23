"""日志链路关联值对象。

按《通用日志开发规范》第六章，日志层**只保留 ``trace_id`` 一个链路关联键**。
本类是该键的不可变载体，供上下文绑定、合并与回填复用。
"""

from dataclasses import dataclass


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
