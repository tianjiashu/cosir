from pydantic import BaseModel, field_validator


class CreateTaskRequest(BaseModel):
    """校验任务创建请求体（仅建 task 容器，不含首轮次）。

    任务创建与首轮次创建已解耦：本请求只携带建 task 容器所需的最小字段
    （``text`` 派生标题）。工作区归属由 URL 路径提供。agent / 模型 / 附件等
    属于 turn 维度的字段由 ``CreateTurnRequest`` 承载，调用方在创建首 turn 时单独传递。

    参数:
        text: 可选的纯文本任务输入；纯附件新对话允许为空。

    返回:
        Pydantic 请求模型。

    异常:
        无。空文本由 workspace API 转换为默认任务标题。

    副作用:
        无。
    """

    text: str = ""

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        """把缺省或空白任务文本归一化为空字符串。"""

        return value.strip()
