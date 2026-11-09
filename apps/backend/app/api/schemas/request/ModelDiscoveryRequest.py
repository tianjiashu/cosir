"""模型目录发现请求。"""

from pydantic import BaseModel, field_validator

from app.api.schemas.request.model_connection_text import normalize_connection_text


class ModelDiscoveryRequest(BaseModel):
    """请求远端模型目录所需的连接字段；Base URL 的路径会被保留，服务层在其后追加 ``/models``。"""

    base_url: str
    api_key: str

    @field_validator("base_url", "api_key")
    @classmethod
    def normalize_text(cls, value: str, info) -> str:
        return normalize_connection_text(value, info.field_name)
