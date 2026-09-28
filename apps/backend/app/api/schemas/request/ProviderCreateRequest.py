from pydantic import BaseModel, model_validator

from app.core.llm_provider.capability.provider_capability import (
    _SUPPORT_PROVIDERS,
    ProviderCapability,
)


class ProviderCreateRequest(BaseModel):
    """校验厂商创建请求体。

    参数:
        name: 能力注册表名称（deepseek、openai、ollama等）；允许多个配置实例复用。
        display_name: 用户可见的配置名称；用于区分同一厂商的不同接入配置。
        model_name: 可选模型名称字段，当前 Provider 创建流程不据此建立模型条目。
        base_url: 接入地址。
        api_key: 可选 API Key 明文（DB 唯一事实来源，本地 SQLite 明文存储；
            ``ollama`` 等不需 Key 的厂商可不填。
        sort_order: 排序权重，默认 0。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当 ``name`` / ``display_name`` 无效、Provider 未注册、或缺少必需的
            API Key / Base URL 时抛出。

    副作用:
        无。
    """

    name: str
    display_name: str
    model_name: str = ""
    base_url: str | None = None
    api_key: str | None = None
    sort_order: int = 0

    @model_validator(mode="after")
    def validate_provider(self) -> "ProviderCreateRequest":
        """归一化并校验 Provider 配置。"""

        self.name = self.name.strip().lower()
        if self.name not in _SUPPORT_PROVIDERS:
            raise ValueError(f"Provider {self.name} is not supported")
        self.display_name = self.display_name.strip()
        if not self.display_name:
            raise ValueError("Provider display_name must not be blank")

        capability = ProviderCapability.get_capability(self.name)
        if capability.requires_api_key and not self.api_key:
            raise ValueError(f"Provider {self.name} requires API Key.")
        if not self.base_url:
            if capability.default_base_url is None:
                raise ValueError(f"Provider {self.name} requires Base URL.")
            self.base_url = capability.default_base_url

        return self
