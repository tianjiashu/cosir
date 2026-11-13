from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.assistant_transport.state.message_identity import parse_reply_id


class ForkTaskRequest(BaseModel):
    """校验手动分叉请求：``replyMessageId`` 是对话界面里一条回复的 wire 消息 id，只收这一个字段。

    不合法时走框架默认的 422（前端只从回复脚注发出合法 id，这条路实际走不到）。
    """

    model_config = ConfigDict(extra="forbid")

    reply_message_id: str = Field(alias="replyMessageId")

    @field_validator("reply_message_id")
    @classmethod
    def _is_a_reply_id(cls, value: str) -> str:
        if parse_reply_id(value) is None:
            raise ValueError("replyMessageId must be a reply id")
        return value

    @property
    def turn(self) -> int:
        """所选回复所在的回合号；校验入口已保证可解码。"""
        return cast(int, parse_reply_id(self.reply_message_id))
