"""``child_agent_send`` 工具的参数模型。"""

from pydantic import BaseModel, Field


class ChildAgentSendArgs(BaseModel):
    """给已有子任务追加输入并启动新 Run 的已校验参数。"""

    child_task_id: int = Field(
        gt=0,
        description=(
            "The child task id returned by the delegate_task_for_sub_agent tool. REQUIRED."
        ),
    )
    message: str = Field(
        min_length=1,
        max_length=8000,
        description=(
            "The follow-up message appended to the existing child task and passed as the "
            "child's next input. REQUIRED, at most 8000 characters."
        ),
    )
