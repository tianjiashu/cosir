"""``child_agent_wait`` 工具的参数模型。"""

from pydantic import BaseModel, Field


class ChildAgentWaitArgs(BaseModel):
    """等待单个子任务最终输出的已校验参数。"""

    child_task_id: int = Field(
        gt=0,
        description=(
            "The child task id returned by the delegate_task tool. REQUIRED."
        ),
    )
    timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        le=3000,
        description=(
            "Maximum time in seconds to wait for the child task's final output. "
            "Defaults to 300s."
        ),
    )
