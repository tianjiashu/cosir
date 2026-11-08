from typing import cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant_transport.request import TransportRequestError
from app.assistant_transport.state.message_identity import parse_reply_id


class ForkTaskRequest(BaseModel):
    """校验手动分叉请求：``replyMessageId`` 是对话界面里一条回复的 wire 消息 id。"""

    model_config = ConfigDict(populate_by_name=True)

    reply_message_id: str = Field(alias="replyMessageId")

    @model_validator(mode="before")
    @classmethod
    def validate_wire(cls, value: object) -> object:
        """将 Fork 请求的纯 wire 校验转换为项目统一的结构化异常。"""

        if (
            not isinstance(value, dict)
            or set(value) != {"replyMessageId"}
            or not isinstance(value["replyMessageId"], str)
            or parse_reply_id(value["replyMessageId"]) is None
        ):
            raise TransportRequestError(
                status_code=422,
                code="FORK_REPLY_ID_REQUIRED",
                message="fork 请求必须提供 replyMessageId",
                retryable=False,
            )
        return value

    @property
    def turn(self) -> int:
        """所选回复所在的回合号；校验入口已保证可解码。"""
        return cast(int, parse_reply_id(self.reply_message_id))
