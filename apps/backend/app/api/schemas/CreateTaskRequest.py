from pydantic import BaseModel, field_validator


class CreateTaskRequest(BaseModel):
    """校验任务创建请求体。

    参数:
        text: 非空的纯文本任务输入。
        session_id: 可选的会话标识。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当 ``text`` 为空白时抛出。

    副作用:
        无。
    """

    text: str
    session_id: str = None

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        """校验任务文本不为空白。

        参数:
            value: 从请求体解析出的文本值。

        返回:
            校验通过时返回原始文本值。

        异常:
            ValueError: 当文本为空白时抛出。

        副作用:
            无。
        """

        if not value.strip():
            raise ValueError("text must not be blank")
        return value