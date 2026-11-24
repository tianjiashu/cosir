"""``session_read`` 工具的参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class SessionReadArgs(BaseModel):
    """读取同工作区一个 Session 的进度。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: int = Field(
        gt=0,
        description=(
            "A session in this workspace: an id from session_list, the child_session_id from "
            "delegate_task, the fork_session_id from fork_session or the source_session_id of an "
            "agent message."
        ),
    )
    steps: int = Field(
        default=8,
        ge=0,
        le=30,
        description="How many recent steps to include (0-30, default 8). Use 0 for status only.",
    )
