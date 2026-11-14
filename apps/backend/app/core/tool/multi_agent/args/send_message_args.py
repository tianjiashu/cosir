"""``send_message`` 工具的参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class SendMessageArgs(BaseModel):
    """给同工作区另一 Session发一条消息的已校验参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: int = Field(
        gt=0,
        description="Recipient session_id in this workspace, e.g. the source_session_id of an agent message.",
    )
    message: str = Field(
        min_length=1,
        max_length=8000,
        description=(
            "The message text, read literally as everything the recipient knows from you. "
            "REQUIRED, at most 8000 characters."
        ),
    )
