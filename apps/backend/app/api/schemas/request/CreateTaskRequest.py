from pydantic import BaseModel, field_validator


class CreateTaskRequest(BaseModel):
    """校验任务创建请求体（仅建 task 容器，不含首轮次）。

    任务创建与首轮次创建已解耦：本请求只携带建 task 容器所需的最小字段
    （``text`` 派生标题、``workspace_id`` 归属工作区）。agent / 模型 / 附件等
    属于 turn 维度的字段由 ``CreateTurnRequest`` 承载，调用方在创建首 turn 时单独传递。

    参数:
        text: 非空的纯文本任务输入。
        workspace_id: 工作区标识。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当 ``text`` 为空白时抛出。

    副作用:
        无。
    """

    text: str
    workspace_id: int

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
