from pydantic import BaseModel, field_validator

from app.api.schemas.request.ProviderCreateRequest import PROVIDER_TYPES


class ProviderUpdateRequest(BaseModel):
    """校验厂商更新请求体（仅覆盖显式传入的字段）。

    置空 ``base_url`` / ``api_key`` 须显式传空字符串 ``""``（与
    None 的「不更新」语义区分）；``api_key`` 传 None 表示不更新、传 ``""`` 表示
    清除；类型校验沿用 ``PROVIDER_TYPES`` 枚举。

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

    name: str | None = None
    type: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None
    sort_order: int | None = None

    @field_validator("type")
    @classmethod
    def _type_must_be_known(cls, value: str | None) -> str | None:
        """校验厂商类型在允许枚举内（None 表示不更新，放行）。

        参数:
            value: 从请求体解析出的类型值；可为 None。

        返回:
            校验通过时返回原始值。

        异常:
            ValueError: 当类型非 None 且不在 ``PROVIDER_TYPES`` 中时抛出。

        副作用:
            无。
        """

        if value is not None and value not in PROVIDER_TYPES:
            raise ValueError(f"type must be one of {PROVIDER_TYPES}")
        return value
