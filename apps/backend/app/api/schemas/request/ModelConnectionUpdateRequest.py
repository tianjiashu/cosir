"""模型连接更新请求。"""

from pydantic import BaseModel, field_validator

from app.api.schemas.request.model_connection_text import normalize_connection_text


class ModelConnectionUpdateRequest(BaseModel):
    """只覆盖显式传入的连接字段；不传 ``api_key`` 表示保留原 Key。"""

    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None

    @field_validator("name", "base_url", "api_key")
    @classmethod
    def normalize_text(cls, value: str | None, info) -> str | None:
        return None if value is None else normalize_connection_text(value, info.field_name)
