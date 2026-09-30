"""模型连接配置表单的公共请求字段。"""

from pydantic import BaseModel, Field, field_validator


class ModelConfigFieldsRequest(BaseModel):
    """校验模型配置表单中创建和连接测试共同需要的字段。

    该模型只负责输入格式与必填字段校验，不负责保存配置或发起模型请求；具体业务由
    API service 层编排。文本字段会去除首尾空白，Base URL 会同时去除末尾斜杠。
    """

    config_name: str
    base_url: str
    api_key: str
    model_name: str
    context_window_k: int = Field(gt=0)
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    supports_image: bool = False

    @field_validator("config_name", "base_url", "api_key", "model_name")
    @classmethod
    def text_must_not_be_blank(cls, value: str, info) -> str:
        """拒绝空白文本，并统一规范化配置名称、地址、Key 和模型名。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be blank")
        return normalized.rstrip("/") if info.field_name == "base_url" else normalized
