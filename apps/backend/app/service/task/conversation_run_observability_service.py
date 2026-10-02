"""Conversation Run 的可观测性事实用例。"""

from app.assistant_transport.event.dispatch import dispatch_conversation_event
from app.assistant_transport.event.run_event import RunTraceUpdatedEvent
from app.models import ConversationRunRecord
from app.service import depends as service_depends


class ConversationRunObservabilityService:
    """持久化 Run 的观测元数据并把它投影到 Transport。

    本服务只负责 Run 与 Langfuse 之间的事实关联，不创建或关闭 Langfuse client，也不改变
    Run 生命周期。写入成功后才发布 ``RunTraceUpdatedEvent``；重复写入同一 trace ID 不重复
    发布事件。
    """

    def __init__(self) -> None:
        """从进程级依赖入口取得 Conversation Run CRUD。"""

        self._run = service_depends.get_conversation_run_crud()

    def set_langfuse_trace_id(self, run_id: int, trace_id: str) -> ConversationRunRecord:
        """记录 Langfuse 根 Trace ID 并更新当前进程的 Transport snapshot。

        参数:
            run_id: 目标 Conversation Run 标识。
            trace_id: Langfuse 生成的非空 trace ID。

        返回:
            写入后的 ``ConversationRunRecord``。

        异常:
            ValueError: trace ID 为空白。
            KeyError: Run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 数据库写入失败。

        副作用:
            更新 Run canonical 事实，并在首次写入新值后直接或经 graph custom stream 发布
            ``RunTraceUpdatedEvent``。本服务不阻断 Langfuse 主流程；调用方应在观测旁路中
            捕获数据库异常。
        """

        current = self._run.get(run_id)
        if current.extra is not None and current.extra.langfuse_trace_id == trace_id:
            return current
        updated = self._run.update_langfuse_trace_id(run_id, trace_id)
        dispatch_conversation_event(
            RunTraceUpdatedEvent(
                task_id=updated.task_id,
                run_id=updated.id,
                trace_id=trace_id,
            )
        )
        return updated
