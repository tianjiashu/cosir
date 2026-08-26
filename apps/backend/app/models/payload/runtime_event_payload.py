"""Runtime event payload capability model."""

from pydantic import BaseModel, ConfigDict


class RuntimeEventPayload(BaseModel):
    """运行时事件 payload 的基础模型。"""

    model_config = ConfigDict(extra="forbid")
