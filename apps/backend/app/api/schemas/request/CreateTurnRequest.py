from pydantic import BaseModel, field_validator


class CreateTurnRequest(BaseModel):
    """校验轮次创建请求体。

    参数:
        input_text: 非空的本轮用户输入。
        provider_id: 可选，模型归属的厂商标识（指向 ``providers.id``）；None 表示未选厂商。
        model_name: 可选，本 turn 关联的模型名；None 表示未选模型。
        reasoning_effort: 可选，本 turn 思考努力等级；None 表示 max。该值经
            ``conversation_run_state_service.create_run`` 透传落库到 ``turns.reasoning_effort``。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当输入为空白或附件列表非法时抛出。

    副作用:
        无。本层只做结构校验，不访问文件系统、不做业务规则判定。
    """

    input_text: str
    model_name: str | None = None
    provider_id: int | None = None
    reasoning_effort: str | None = None

    @field_validator("input_text")
    @classmethod
    def input_text_must_not_be_blank(cls, value: str) -> str:
        """校验本轮输入不为空白。

        参数:
            value: 从请求体解析出的输入文本。

        返回:
            去除首尾空白后的输入文本。

        异常:
            ValueError: 当输入为空白时抛出。

        副作用:
            无。
        """

        if not value or not value.strip():
            raise ValueError("input_text must not be blank")
        value = value.strip()
        if len(value) > 1000:
            raise ValueError("input_text must be less than 1000 characters")
        return value

