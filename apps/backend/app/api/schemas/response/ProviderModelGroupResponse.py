"""按模型厂商聚合的模型目录响应结构。"""

from pydantic import BaseModel

from app.api.schemas.response.ModelListItemResponse import ModelListItemResponse


class ProviderModelGroupResponse(BaseModel):
    """描述一个厂商及其模型目录。"""

    provider_id: int
    provider_name: str
    models: list[ModelListItemResponse]
