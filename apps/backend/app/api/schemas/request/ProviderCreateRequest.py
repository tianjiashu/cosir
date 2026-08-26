from pydantic import BaseModel, field_validator
from app.llm_provider.capability.provider_capability import _SUPPORT_PROVIDERS, ProviderCapability


class ProviderCreateRequest(BaseModel):
    """校验厂商创建请求体。

    参数:
        name: 厂商显示名（全局唯一，deepseek、openai、ollama等）。
        model_name: 模型名称,仅当name 为 custome 或 ollama 时有效。
        base_url: 接入地址。
        api_key: 可选 API Key 明文（DB 唯一事实来源，本地 SQLite 明文存储；
            ``ollama`` 等不需 Key 的厂商可不填。
        enabled: 启用开关，默认 True。
        sort_order: 排序权重，默认 0。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当 ``name`` / ``type`` 为空白或 ``type`` 不在允许枚举时抛出。

    副作用:
        无。
    """

    name: str
    model_name: str
    base_url: str | None = None
    api_key: str | None = None
    sort_order: int = 0

    def __post_init__(self):
        self.name = self.name.strip().lower()
        if self.name not in _SUPPORT_PROVIDERS:
            raise ValueError(f"Provider {self.name} is not supported.")

        if self.name != "ollama" and not self.api_key:
            raise ValueError(f"Provider {self.name} requires API Key.")
        if not self.base_url:
            capability: ProviderCapability = ProviderCapability.get_capability(self.name)
            if capability is None:
                raise ValueError(f"Provider {self.name} requires Base URL.")
            self.base_url = capability.default_base_url

        if self.name in ["custome", "ollama"] and not self.model_name:
            raise ValueError(f"Provider {self.name} requires Model Name.")
