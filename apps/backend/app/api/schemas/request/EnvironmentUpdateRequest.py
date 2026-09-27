"""环境文件批量更新请求体。"""

from pydantic import BaseModel, ConfigDict

from app.api.schemas.request.EnvironmentChangeRequest import EnvironmentChangeRequest


class EnvironmentUpdateRequest(BaseModel):
    """校验环境文件批量更新请求体。

    参数:
        changes: 字段名到变更意图的映射；未出现的字段保持原值。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 字段类型不符或携带额外字段时由 Pydantic 校验抛出。

    副作用:
        无（只做结构校验，不读写文件）。
    """

    model_config = ConfigDict(extra="forbid")

    changes: dict[str, EnvironmentChangeRequest]
