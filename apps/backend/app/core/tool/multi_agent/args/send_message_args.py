"""``send_message`` 工具的参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class SendMessageArgs(BaseModel):
    """给同工作区另一 Task发一条消息的已校验参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    task_id: int = Field(
        gt=0,
        description="Recipient task_id in this workspace, e.g. the source_task_id of an agent message.",
    )
    message: str = Field(
        min_length=1,
        max_length=8000,
        description="The message text. REQUIRED, at most 8000 characters.",
    )
