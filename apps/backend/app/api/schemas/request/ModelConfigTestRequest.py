"""未保存模型的连接测试请求。"""

from pydantic import field_validator

from app.api.schemas.request.model_connection_text import normalize_connection_text
from app.api.schemas.request.ModelConfigFieldsRequest import ModelConfigFieldsRequest


class ModelConfigTestRequest(ModelConfigFieldsRequest):
    """一个模型的字段加上它要走的连接地址与 Key；不落库。

    能力声明会改变请求参数，所以测试要带上与保存时相同的能力，测的是「这个模型按这组能力」。
    """

    base_url: str
    api_key: str

    @field_validator("base_url", "api_key")
    @classmethod
    def normalize_text(cls, value: str, info) -> str:
        return normalize_connection_text(value, info.field_name)
