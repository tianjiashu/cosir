from pydantic import BaseModel, field_validator


class ProviderUpdateRequest(BaseModel):
    """校验厂商更新请求体（仅覆盖显式传入的字段）。

    参数:
        name: 可选，新显示名。
        type: 可选，新厂商类型（枚举校验）。
        base_url: 可选，新接入地址；``""`` 表示清除。
        api_key: 可选，新 API Key 明文；``None`` 不更新，``""`` 清除。
        enabled: 可选，新启用状态（启停即时生效）。
        sort_order: 可选，新排序权重。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当 ``type`` 非空且不在允许枚举时抛出（由 pydantic 校验触发）。

    副作用:
        无。
    """

    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    sort_order: int | None = None
