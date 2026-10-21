"""执行事件的共有信封。"""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class WorkflowEventEnvelope(BaseModel):
    """所有执行事件共有的身份与时间字段。

    事件只描述已经发生的事实，不含任何投影逻辑；如何把事实呈现给某个消费者（例如 Assistant
    Transport snapshot）由消费方自己的投影层决定。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: int = Field(ge=1)
    # 所有当前事件都属于某个 Run；保留可空类型以复用底层事件信封契约。
    run_id: int | None = None
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    step_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
