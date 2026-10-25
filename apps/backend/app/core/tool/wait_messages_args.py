"""``wait_messages`` 工具的参数模型：没有参数，等什么由挂起时 Task 上的未完成工作决定。"""

from pydantic import BaseModel, ConfigDict


class WaitMessagesArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
