from __future__ import annotations

from app.config.settings import Settings
from app.llm_provider.model_catalog import ModelCatalog


def resolve_context_window(model_name: str) -> int:
    """计算实际上下文窗口上限（token）。

    参数:
        model_name: 当前目标模型名（如 ``deepseek-v4-flash``）。

    返回:
        实际可用上下文窗口上限（token）= min(模型最大窗口, 全局软上限)；
        软上限为 0 时只用模型最大窗口。

    异常:
        无。

    副作用:
        无。
    """
    model_max = ModelCatalog.max_context_window(model_name)
    soft_cap = Settings.CONTEXT_WINDOW_TOKENS
    if soft_cap <= 0:
        return model_max
    return min(model_max, soft_cap)
