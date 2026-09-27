"""全局指令更新请求体。"""

from pydantic import BaseModel, ConfigDict


class GlobalInstructionUpdateRequest(BaseModel):
    """校验全局指令更新请求体。

    参数:
        content: 新的全局指令正文。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 字段类型不符或携带额外字段时由 Pydantic 校验抛出。

    副作用:
        无（只做结构校验，不读写文件）。
    """

    model_config = ConfigDict(extra="forbid")

    content: str
