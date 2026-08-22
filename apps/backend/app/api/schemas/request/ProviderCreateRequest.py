from pydantic import BaseModel, field_validator

from app.models.provider_capability import get_provider_types

#: 允许的厂商类型枚举（决定 litellm 前缀与默认 base_url）。
#:
#: 从 ``ProviderCapability`` 静态注册表派生（设计文档 §三），保持单一事实源——
#: 新增厂商 = 改注册表一行，本枚举自动同步，无需手改。初始覆盖 15 类：
#: deepseek / openai-compatible / anthropic / gemini / azure / dashscope /
#: moonshot / zai / volcengine / tencent / minimax / ollama / qianfan / xfyun /
#: custom（详见 ``app/models/provider_capability.py``）。
PROVIDER_TYPES: tuple[str, ...] = get_provider_types()


class ProviderCreateRequest(BaseModel):
    """校验厂商创建请求体。

    参数:
        name: 厂商显示名（全局唯一，非空白）。
        type: 厂商类型（必须命中 ``PROVIDER_TYPES`` 注册表键，如 ``deepseek`` /
            ``azure`` / ``zai`` / ``dashscope`` / ``custom`` 等 15 类）。
        base_url: 可选自定义接入地址；空时走 ``ProviderCapability.default_base_url``
            或交 litellm 内置解析。
        api_key: 可选 API Key 明文（DB 唯一事实来源，本地 SQLite 明文存储；
            响应与日志不回传明文——见 ``ProviderRecord.to_dict`` no-leak 约束）。
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
    type: str
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool = True
    sort_order: int = 0

    @field_validator("name", "type")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        """校验文本字段不为空白。

        参数:
            value: 从请求体解析出的字段值。

        返回:
            校验通过时返回原始值。

        异常:
            ValueError: 当字段为空白时抛出。

        副作用:
            无。
        """

        if not value.strip():
            raise ValueError("field must not be blank")
        return value

    @field_validator("type")
    @classmethod
    def _type_must_be_known(cls, value: str) -> str:
        """校验厂商类型在允许枚举内。

        参数:
            value: 从请求体解析出的类型值。

        返回:
            校验通过时返回原始值。

        异常:
            ValueError: 当类型不在 ``PROVIDER_TYPES`` 中时抛出。

        副作用:
            无。
        """

        if value not in PROVIDER_TYPES:
            raise ValueError(f"type must be one of {PROVIDER_TYPES}")
        return value
