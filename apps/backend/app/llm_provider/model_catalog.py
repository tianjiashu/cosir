"""模型事实目录：模型名 → 最大上下文窗口的单一事实来源。

单一职责：集中维护各模型的最大上下文窗口（token），供上下文占用计算的
``min(模型最大窗口, 全局软上限)`` 查表。窗口是模型的事实属性，不应散落在每个
AgentProfile 构造里重复抄写；新增模型时在此追加一行即可，计算代码无需改动。

不负责：全局软上限（归 Settings）、实际窗口的 min 计算（归 context_window_resolver）、
模型配置的加载（归 ModelSettings）。
"""

from __future__ import annotations

from typing import ClassVar

from app.config.logging.logger import log

# 模型名 → 最大上下文窗口（token）。DeepSeek-V4 全系官方标配 1M 上下文，
# 精确覆盖优先于 litellm 目录查询。
_DEFAULT_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
}

# 未收录模型的兜底窗口（保守值，宁可低估不误导用户以为余量很大）。
_FALLBACK_CONTEXT_WINDOW: int = 128_000

#: 进程内缓存未命中的哨兵（区别于「已缓存但值为 None = 目录查询失败」，
#: 避免把失败结果当未命中导致每次重复查 litellm 刷日志）。
_MISS = object()


class ModelCatalog:
    """模型事实目录：按模型名查最大上下文窗口。

    单一职责：集中维护各模型的最大上下文窗口（token），供上下文占用计算的
    ``min(模型最大窗口, 全局软上限)`` 查表。窗口是模型的事实属性，不应散落在每个
    AgentProfile 构造里重复抄写；新增模型时在此追加一行即可，计算代码无需改动。

    不负责：全局软上限（归 Settings）、实际窗口的 min 计算（归 context_window_resolver）、
    模型配置的加载（归 ModelSettings）。

    litellm 目录回填（设计文档阶段 5 ①）：未知模型懒查 litellm 目录
    （``get_model_info``）回填 ``max_input_tokens``，deepseek 精确覆盖保留，
    目录查询失败静默回退兜底窗口（不抛，避免观测链路被拖垮）。
    """

    #: 懒查 litellm 目录的进程内缓存：裸模型名 → 窗口或 None（None=目录查询失败）。
    _litellm_window_cache: ClassVar[dict[str, int | None]] = {}

    @classmethod
    def max_context_window(cls, model_name: str) -> int:
        """返回模型的最大上下文窗口（token）；未知模型经 litellm 目录回填。

        查表前先对 ``model_name`` 做 ``rsplit("/", 1)[-1]`` 归一化取裸名：带 provider
        前缀名（如 ``deepseek/deepseek-v4-flash``）与裸名（``deepseek-v4-flash``）
        均命中同一事实表项。命中顺序：① deepseek 精确覆盖；② litellm 目录懒查
        ``max_input_tokens``（进程内缓存，目录查询失败记一次 warn 并回退兜底）；
        ③ 未收录 / 查询失败返回 128_000 兜底。

        参数:
            model_name: 模型名（带前缀或裸名均可，如 ``deepseek/deepseek-v4-flash``
                或 ``deepseek-v4-flash``）。

        返回:
            该模型的最大上下文窗口 token 数；未收录 / 目录查询失败时返回 128_000 兜底。

        异常:
            无（litellm 目录查询异常在此吞掉，回退兜底窗口）。

        副作用:
            目录查询失败写一次 warn 级 ``model_catalog_litellm_lookup_failed`` 日志；
            命中结果写进程内缓存（重复查询不再打日志 / 不再调 litellm）。
        """
        normalized = model_name.rsplit("/", 1)[-1]
        exact = _DEFAULT_CONTEXT_WINDOWS.get(normalized)
        if exact is not None:
            return exact
        cached = cls._litellm_window_cache.get(normalized, _MISS)
        if cached is not _MISS:
            return cached if cached is not None else _FALLBACK_CONTEXT_WINDOW
        resolved = cls._lookup_litellm_window(normalized)
        cls._litellm_window_cache[normalized] = resolved
        return resolved if resolved is not None else _FALLBACK_CONTEXT_WINDOW

    @classmethod
    def _lookup_litellm_window(cls, normalized: str) -> int | None:
        """懒查 litellm 目录获取模型最大输入窗口；失败静默回退。

        参数:
            normalized: 归一化后的裸模型名。

        返回:
            litellm 目录中的 ``max_input_tokens``；未收录或查询异常返回 None。

        异常:
            无（litellm 异常在此捕获并降级）。

        副作用:
            查询失败写一次 warn 级日志（带模型名，便于排查）。
        """
        try:
            from litellm import get_model_info

            info = get_model_info(normalized)
        except Exception:
            log.warning(
                "model_catalog_litellm_lookup_failed",
                extra={
                    "msg": "litellm catalog lookup failed; falling back to default window",
                    "data": {"model": normalized},
                },
                exc_info=True,
            )
            return None
        if not isinstance(info, dict):
            return None
        window = info.get("max_input_tokens")
        return window if isinstance(window, int) and window > 0 else None