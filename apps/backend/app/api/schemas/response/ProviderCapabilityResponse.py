"""可配置 Provider 能力目录响应。"""

from pydantic import BaseModel


class ProviderCapabilityResponse(BaseModel):
    """描述能力注册表中一个可配置的 Provider。"""

    name: str
    provider_type: str
    default_base_url: str | None = None
    requires_api_key: bool = True
    models: list[str]
