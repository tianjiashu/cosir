"""``task_status`` 工具的参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class TaskStatusArgs(BaseModel):
    """读取自己派生的 Task 当前状态的已校验参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    task_id: int = Field(
        gt=0,
        description=(
            "A task you started: the child_task_id from delegate_task or the fork_task_id from "
            "fork_task. REQUIRED."
        ),
    )
