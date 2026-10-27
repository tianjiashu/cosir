from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant_transport.request import TransportRequestError


class ForkTaskRequest(BaseModel):
    """校验按消息切分的 fork 请求：``messageId`` 是对话界面里的 wire 消息 id。"""

    model_config = ConfigDict(populate_by_name=True)

    message_id: str = Field(alias="messageId")

    @model_validator(mode="before")
    @classmethod
    def validate_wire(cls, value: object) -> object:
        """将 Fork 请求的纯 wire 校验转换为项目统一的结构化异常。"""

        if (
            not isinstance(value, dict)
            or not isinstance(value.get("messageId"), str)
            or not value["messageId"].strip()
        ):
            raise TransportRequestError(
                status_code=422,
                code="FORK_MESSAGE_ID_REQUIRED",
                message="fork 请求必须提供 messageId",
                retryable=False,
            )
        return value
