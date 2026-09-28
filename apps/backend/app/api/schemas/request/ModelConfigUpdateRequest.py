"""模型连接配置更新请求。"""

from pydantic import BaseModel, Field, field_validator


class ModelConfigUpdateRequest(BaseModel):
    """只覆盖显式传入的模型连接配置字段。"""

    config_name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model_name: str | None = None
    context_window_k: int | None = Field(default=None, gt=0)
    enabled: bool | None = None
    sort_order: int | None = None

    @field_validator("config_name", "base_url", "api_key", "model_name")
    @classmethod
    def normalize_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be blank")
        return normalized.rstrip("/") if info.field_name == "base_url" else normalized
