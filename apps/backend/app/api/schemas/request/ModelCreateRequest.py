from pydantic import BaseModel, field_validator


class ModelCreateRequest(BaseModel):
    """校验单条模型条目的创建/导入请求体（手动添加与批量导入的条目单元）。

    参数:
        model_name: litellm 路由名（如 ``deepseek/deepseek-v4-flash``，非空白，
            厂商内唯一）。
        display_name: 下拉展示名（非空白，可省略 provider 前缀）。
        max_context_window: 上下文窗口（token，正整数）。
        supports_thinking: 推理模型标识，默认 False。
        enabled: 启用开关，默认 True（下拉只显示启用项）。
        sort_order: 组内排序权重，默认 0。

    返回:
        Pydantic 请求模型。

    异常:
        ValueError: 当 ``model_name`` / ``display_name`` 为空白或
            ``max_context_window`` 非正整数时抛出。

    副作用:
        无。
    """

    model_name: str
    display_name: str
    max_context_window: int
    supports_thinking: bool = False
    enabled: bool = True
    sort_order: int = 0

    @field_validator("model_name", "display_name")
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

    @field_validator("max_context_window")
    @classmethod
    def _window_must_be_positive(cls, value: int) -> int:
        """校验上下文窗口为正整数。

        参数:
            value: 从请求体解析出的窗口值。

        返回:
            校验通过时返回原始值。

        异常:
            ValueError: 当窗口非正时抛出。

        副作用:
            无。
        """

        if value <= 0:
            raise ValueError("max_context_window must be positive")
        return value
