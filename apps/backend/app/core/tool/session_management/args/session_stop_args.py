"""``session_stop`` 工具的参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class SessionStopArgs(BaseModel):
    """停止一个自己派生的 Session。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: int = Field(
        gt=0,
        description="A session you derived directly (marked stop in session_list), or an id from delegate_task or fork_session.",
    )
