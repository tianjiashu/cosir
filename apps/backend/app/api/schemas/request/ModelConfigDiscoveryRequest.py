"""模型目录发现请求。"""

from pydantic import BaseModel, field_validator


class ModelConfigDiscoveryRequest(BaseModel):
    """校验请求远端模型目录所需的连接字段。

    本模型只负责清洗 Base URL 和 API Key，不负责发起网络请求、保存配置或判断模型能力。
    Base URL 的路径会被保留，服务层在其后追加 ``/models``。
    """

    base_url: str
    api_key: str

    @field_validator("base_url", "api_key")
    @classmethod
    def text_must_not_be_blank(cls, value: str, info) -> str:
        """拒绝空白连接字段，并去除 Base URL 末尾斜杠。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be blank")
        return normalized.rstrip("/") if info.field_name == "base_url" else normalized
