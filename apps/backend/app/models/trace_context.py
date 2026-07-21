"""Trace 运行态上下文值对象。

单一职责：承载一次任务运行中的 trace 关联字段，并支持派生子 span 上下文。
不负责 ID 生成算法（由 ``app.trace_infra.ids`` 负责）。
"""

from dataclasses import dataclass

from app.trace_infra.ids import new_span_id


@dataclass(frozen=True)
class TraceContext:
    """描述一次任务运行中的 trace 关联字段。

    参数:
        trace_id: 全链路 trace 标识。
        task_id: 任务标识。
        run_id: Durable Run 标识，可为空。
        span_id: 当前 span 标识，可为空。
        parent_span_id: 父 span 标识，可为空。

    返回:
        不可变 trace 上下文。

    异常:
        无。

    副作用:
        无。
    """

    trace_id: str
    task_id: str
    run_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""

    def child_span(self) -> "TraceContext":
        """派生一个子 span 上下文。

        参数:
            无。

        返回:
            共享 trace/task/run 的新 TraceContext，当前 span 成为父 span。

        异常:
            无。

        副作用:
            生成新的 span_id。
        """

        return TraceContext(
            trace_id=self.trace_id,
            task_id=self.task_id,
            run_id=self.run_id,
            span_id=new_span_id(),
            parent_span_id=self.span_id,
        )
