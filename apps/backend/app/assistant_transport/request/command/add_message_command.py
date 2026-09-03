
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.request.command.assistant_user_message import AssistantUserMessage


class AddMessageCommand(BaseModel):
    """校验首版支持的 ``add-message`` 命令。"""

    model_config = ConfigDict(extra="ignore")

    type: Literal["add-message"]
    commandId: str = Field(min_length=1, max_length=128)
    message: AssistantUserMessage
    parentId: str | None = None
    sourceId: str | None = None
