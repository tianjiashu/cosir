"""Session 自身属性变化的事件。"""

from typing import Literal

from pydantic import Field

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope


class SessionTitleUpdatedEvent(WorkflowEventEnvelope):
    """Session 标题已写入 ``sessions.title``（事实源），消费方据此刷新投影。

    由 Run 的标题支线在写库成功后发出；``run_id`` 只标明是哪次驱动生成的，标题本身属于 Session。
    按 ``title`` 覆盖，可重复投递。
    """

    type: Literal["session_title_updated"] = "session_title_updated"
    title: str = Field(min_length=1)


class SessionQueueChangedEvent(WorkflowEventEnvelope):
    """Session 的外部队列有了新消息（已提交），消费方据此整表刷新排队投影。

    由 Session 的调度 actor 在醒来读到新消息时发出：入队方可能在自己的事务里写入，提交后才由 actor 读到，
    所以由它统一发出，而不是由各入队方。不属于任何 Run（``run_id`` 为空）；整表刷新，可重复投递。
    """

    type: Literal["session_queue_changed"] = "session_queue_changed"
