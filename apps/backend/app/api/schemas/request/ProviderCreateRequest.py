from pydantic import BaseModel, field_validator


class ProviderCreateRequest(BaseModel):
    """校验厂商创建请求体。

    参数:
        name: 厂商显示名（全局唯一，deepseek、openai、ollama等）。
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
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool = True
    sort_order: int = 0
