"""模型连接配置创建请求。"""

from pydantic import BaseModel, Field, field_validator


class ModelConfigCreateRequest(BaseModel):
    """校验模型连接配置的连接字段和必填上下文窗口。"""

    config_name: str
    base_url: str
    api_key: str
    model_name: str
    context_window_k: int = Field(gt=0)
    sort_order: int = 0

    @field_validator("config_name", "base_url", "api_key", "model_name")
    @classmethod
    def text_must_not_be_blank(cls, value: str, info) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be blank")
        return normalized.rstrip("/") if info.field_name == "base_url" else normalized
