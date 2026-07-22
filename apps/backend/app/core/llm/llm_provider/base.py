"""LLM Provider 抽象基类。

所有模型服务商适配器都实现 ``build()`` 并返回 LangChain ``BaseChatModel``；流式与非流式由
返回的模型对象天然支持（``.astream()`` / ``.ainvoke()``），不需要提供商自己再做流解析。
"""

from abc import ABC, abstractmethod

from langchain_core.language_models import BaseChatModel

from app.core.llm.model_settings import ModelSettings
from app.core.llm.registry import ModelSpec


class LLMProvider(ABC):
    """LLM Provider 抽象基类——所有模型适配器必须实现 ``build``。

    返回的模型对象同时支持流式（``.astream()``）与非流式（``.ainvoke()``）调用，由调用方
    （workflow / graph 节点）决定输入输出形态；provider 不负责流解析或消息/工具格式转换。
    """

    @abstractmethod
    def build(
        self, spec: ModelSpec, model_settings: ModelSettings | None = None
    ) -> BaseChatModel:
        """按规格构建 LangChain chat model。

        参数:
            spec: 由模型注册表解析出的 ``ModelSpec``（含 base_url / api_key_env / thinking 等）。
            model_settings: 可选 Agent 级模型覆盖配置（生成参数）。

        返回:
            一个 LangChain ``BaseChatModel`` 实例。
        """
        ...
