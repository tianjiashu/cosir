"""新建模型连接请求：连接与它的首批模型一次提交。"""

from pydantic import BaseModel, field_validator

from app.api.schemas.request.model_connection_text import normalize_connection_text
from app.api.schemas.request.ModelConfigFieldsRequest import ModelConfigFieldsBatch


class ModelConnectionCreateRequest(BaseModel):
    """至少带一个模型：没有模型的连接在选择器里什么也不提供，新建时不允许。"""

    name: str
    base_url: str
    api_key: str
    models: ModelConfigFieldsBatch

    @field_validator("name", "base_url", "api_key")
    @classmethod
    def normalize_text(cls, value: str, info) -> str:
        return normalize_connection_text(value, info.field_name)
