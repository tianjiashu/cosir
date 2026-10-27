"""Task 自身属性变化的事件。"""

from typing import Literal

from pydantic import Field

from app.core.events.workflow_event_envelope import WorkflowEventEnvelope


class TaskTitleUpdatedEvent(WorkflowEventEnvelope):
    """Task 标题已写入 ``tasks.title``（事实源），消费方据此刷新投影。

    由 Run 的标题支线在写库成功后发出；``run_id`` 只标明是哪次驱动生成的，标题本身属于 Task。
    按 ``title`` 覆盖，可重复投递。
    """

    type: Literal["task_title_updated"] = "task_title_updated"
    title: str = Field(min_length=1)
