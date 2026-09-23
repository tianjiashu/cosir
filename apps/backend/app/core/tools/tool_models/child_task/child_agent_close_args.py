"""``child_agent_close`` 工具的参数模型。"""

from pydantic import BaseModel, Field


class ChildAgentCloseArgs(BaseModel):
    """幂等关闭 Child Agent 的已校验参数。"""

    child_task_id: int = Field(
        gt=0,
        description=(
            "The child task id returned by the delegate_task_for_sub_agent tool. REQUIRED."
        ),
    )
