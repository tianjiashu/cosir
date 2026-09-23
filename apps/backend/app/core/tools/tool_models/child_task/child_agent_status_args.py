"""``child_agent_status`` 工具的参数模型。"""

from pydantic import BaseModel, Field


class ChildAgentStatusArgs(BaseModel):
    """canonical Child Agent 状态读取的已校验参数。"""

    child_task_id: int = Field(
        gt=0,
        description=(
            "The child task id returned by the delegate_task tool. REQUIRED."
        ),
    )
