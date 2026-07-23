from pydantic import BaseModel, field_validator


class CreateTurnRequest(BaseModel):
    """校验轮次创建请求体。

    参数:
        input_text: 非空的本轮用户输入。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当输入为空白时抛出。

    副作用:
        无。
    """

    input_text: str
    agent_id: str | None = None

    @field_validator("input_text")
    @classmethod
    def input_text_must_not_be_blank(cls, value: str) -> str:
        """校验本轮输入不为空白。

        参数:
            value: 从请求体解析出的输入文本。

        返回:
            原始输入文本。

        异常:
            ValueError: 当输入为空白时抛出。

        副作用:
            无。
        """

        if not value.strip():
            raise ValueError("input_text must not be blank")
        return value
