"""``send_message`` 工具的参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class SendMessageArgs(BaseModel):
    """给自己派生的 Task 或派生自己的那一方发一条消息的已校验参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    task_id: int = Field(
        gt=0,
        description=(
            "Who to message: a task you started (the child_task_id from delegate_task or the "
            "fork_task_id from fork_task), or the agent that started you (the source_task_id on "
            "its <agent_message>). REQUIRED."
        ),
    )
    message: str = Field(
        min_length=1,
        max_length=8000,
        description="The message text. REQUIRED, at most 8000 characters.",
    )
    expect_reply: bool | None = Field(
        default=None,
        description=(
            "Only when messaging the agent that started you: true if you need its answer before "
            "you can go on; the reply then arrives as a <background_result> for this call, so "
            "call wait_messages to pause for it. false or null just tells it something. Ignored "
            "for tasks you started: their next conclusion always comes back to you."
        ),
    )
