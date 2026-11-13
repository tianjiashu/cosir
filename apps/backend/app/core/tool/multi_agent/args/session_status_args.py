"""``session_status`` 工具的参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class SessionStatusArgs(BaseModel):
    """读取自己派生的 Session 当前状态的已校验参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: int = Field(
        gt=0,
        description=(
            "A session you started: the child_session_id from delegate_task or the fork_session_id from "
            "fork_session. REQUIRED."
        ),
    )
