from pydantic import BaseModel, field_validator


class CreateTaskRequest(BaseModel):
    """校验任务创建请求体。

    参数:
        text: 非空的纯文本任务输入。
        workspace_id: 工作区标识。
        model_name: 可选，本任务首个 turn 请求的模型名（设计 §8.3，D1 配套）；
            None 表示 Auto（跟随 Agent 默认模型）。该值经
            ``task_service.create_task_with_initial_turn`` 透传至首 turn 落库。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当 ``text`` 为空白时抛出。

    副作用:
        无。
    """

    text: str
    agent_id: str
    workspace_id: int
    model_name: str | None = None

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
