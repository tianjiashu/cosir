"""上下文窗口上限解析：实际上限 = min(模型最大窗口, 全局软上限) 的单一事实来源。

单一职责：集中计算「当前任务上下文窗口上限（token）」。模型最大窗口归 ``ModelCatalog``
（事实目录），全局软上限归 ``Settings.CONTEXT_WINDOW_TOKENS``（0 表示不设限）。窗口解析是
模型的事实属性，不应散落在 workflow 编排或计量器内部重复实现；本模块是其唯一收口点，
``ContextUsageMeter`` 与 ``ReactLikeWorkflow`` 均调用此处，避免规则漂移。

不负责：token 估算（归 TokenEstimator）、消息采集（归 RuntimeContext）、上下文占用计量
（归 ContextUsageMeter）。
"""

from __future__ import annotations

from app.config.settings import Settings
from app.core.llm.model_catalog import ModelCatalog


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
