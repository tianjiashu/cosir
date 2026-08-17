"""模型事实目录：模型名 → 最大上下文窗口的单一事实来源。

单一职责：集中维护各模型的最大上下文窗口（token），供上下文占用计算的
``min(模型最大窗口, 全局软上限)`` 查表。窗口是模型的事实属性，不应散落在每个
AgentProfile 构造里重复抄写；新增模型时在此追加一行即可，计算代码无需改动。

不负责：全局软上限（归 Settings）、实际窗口的 min 计算（归 ContextUsageMeter）、
模型配置的加载（归 ModelSettings）。
"""

from __future__ import annotations

# 模型名 → 最大上下文窗口（token）。DeepSeek-V4 全系官方标配 1M 上下文。
_DEFAULT_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
}

# 未收录模型的兜底窗口（保守值，宁可低估不误导用户以为余量很大）。
_FALLBACK_CONTEXT_WINDOW: int = 128_000


class ModelCatalog:
    """模型事实目录：按模型名查最大上下文窗口。"""

    @classmethod
    def max_context_window(cls, model_name: str) -> int:
        """返回模型的最大上下文窗口（token）；未收录模型返回保守兜底值。

        查表前先对 ``model_name`` 做 ``rsplit("/", 1)[-1]`` 归一化取裸名：带 provider
        前缀名（如 ``deepseek/deepseek-v4-flash``）与裸名（``deepseek-v4-flash``）
        均命中同一事实表项；未收录时返回 128_000 兜底。

        参数:
            model_name: 模型名（带前缀或裸名均可，如 ``deepseek/deepseek-v4-flash``
                或 ``deepseek-v4-flash``）。

        返回:
            该模型的最大上下文窗口 token 数；未收录时返回 128_000 兜底。

        异常:
            无。

        副作用:
            无。
        """
        normalized = model_name.rsplit("/", 1)[-1]
        return _DEFAULT_CONTEXT_WINDOWS.get(normalized, _FALLBACK_CONTEXT_WINDOW)
