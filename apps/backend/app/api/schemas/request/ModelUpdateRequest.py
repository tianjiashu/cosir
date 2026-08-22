from pydantic import BaseModel, field_validator


class ModelUpdateRequest(BaseModel):
    """校验模型条目更新请求体（仅覆盖显式传入的字段）。

    参数:
        display_name: 可选，新展示名（非空白）。
        max_context_window: 可选，新上下文窗口（正整数）。
        supports_thinking: 可选，新推理模型标识。
        enabled: 可选，新启用状态（下拉只显示启用项）。
        sort_order: 可选，新排序权重。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当 ``display_name`` 为空白或 ``max_context_window`` 非正
            整数时抛出（由 pydantic 校验触发）。

    副作用:
        无。
    """

    display_name: str | None = None
    max_context_window: int | None = None
    supports_thinking: bool | None = None
    enabled: bool | None = None
    sort_order: int | None = None

    @field_validator("display_name")
    @classmethod
    def _display_name_must_not_be_blank(cls, value: str | None) -> str | None:
        """校验展示名非空时不为空白（None 表示不更新，放行）。

        参数:
            value: 从请求体解析出的展示名；可为 None。

        返回:
            校验通过时返回原始值。

        异常:
            ValueError: 当展示名非 None 且为空白时抛出。

        副作用:
            无。
        """

        if value is not None and not value.strip():
            raise ValueError("display_name must not be blank")
        return value

    @field_validator("max_context_window")
    @classmethod
    def _window_must_be_positive(cls, value: int | None) -> int | None:
        """校验窗口非空时为正整数（None 表示不更新，放行）。

        参数:
            value: 从请求体解析出的窗口值；可为 None。

        返回:
            校验通过时返回原始值。

        异常:
            ValueError: 当窗口非 None 且非正时抛出。

        副作用:
            无。
        """

        if value is not None and value <= 0:
            raise ValueError("max_context_window must be positive")
        return value
