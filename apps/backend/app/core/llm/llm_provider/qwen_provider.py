"""Qwen 模型提供商——基于 LangChain OpenAI 兼容客户端构建 chat model。

单一职责：把阿里云百炼（DashScope）OpenAI 兼容端点的 base_url / api_key / thinking 差异
封装为 ``langchain_openai.ChatOpenAI`` 实例，与 ``DeepSeekProvider`` 平级。所有流式解析、
工具调用累积、消息格式转换交给 LangChain + ``core/llm/langchain_bridge`` + LangGraph，
本类不重复实现。

DeepSeek 与 Qwen 均为 OpenAI 兼容 provider，共用 ``openai_compatible`` 里的构建基座
（``reasoning_content`` 收拢 + 构造样板），本类只保留 Qwen 的端点回落链与日志标识。

``DASHSCOPE_API_KEY`` 必须走环境变量，禁止硬编码；端点默认
``https://dashscope.aliyuncs.com/compatible-mode/v1``，带 Workspace 前缀的隔离端点由
``QWEN_BASE_URL`` 环境变量整体携带。
"""

from os import environ

from langchain_core.language_models import BaseChatModel

from app.core.llm.llm_provider.base import LLMProvider
from app.core.llm.llm_provider.openai_compatible import (
    OpenAICompatibleChatOpenAI,
    build_openai_compatible_chat_model,
)
from app.core.llm.model_settings import ModelSettings

_DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_QWEN_BASE_URL_ENV = "QWEN_BASE_URL"


class QwenChatOpenAI(OpenAICompatibleChatOpenAI):
    """Qwen 兼容的 ``ChatOpenAI``（继承共享基座以收拢 ``reasoning_content``）。

    构造、流式解析与思考内容收拢均由 ``OpenAICompatibleChatOpenAI`` 基座提供；本类仅以
    明确类型标识 Qwen 的模型实例，不重复实现任何转换逻辑。
    """


class QwenProvider(LLMProvider):
    """Qwen 模型适配器，基于 LangChain OpenAI 兼容客户端（DashScope compatible-mode）。

    返回的模型对象由 LangChain/LangGraph 负责流式输出、工具调用累积与思考内容处理；本类只做
    构造，把端点 / Key / 采样参数封装进 ``QwenChatOpenAI``。
    """

    def build(self, model_name: str, model_settings: ModelSettings | None = None) -> BaseChatModel:
        """构建面向 Qwen（DashScope）的 LangChain chat model。

        参数:
            model_name: 模型名称，例如 ``qwen3.7-plus`` / ``qwen-vl-max``。
            model_settings: 可选 Agent 级模型覆盖配置（base_url / api_key_env / thinking /
                采样参数）。其中某生成参数被显式提供时覆盖对应默认构造行为；未提供的参数
                不传入（沿用模型 / 客户端默认）。base_url 缺省时回落到
                ``QWEN_BASE_URL`` 环境变量，再回落 ``_DEFAULT_QWEN_BASE_URL``。

        返回:
            配置好 base_url / api_key / thinking / 采样参数的 ``QwenChatOpenAI`` 实例。

        异常:
            无（缺 API Key 时以 ``api_key=None`` 构造，由 factory 的统一回退兜底）。

        副作用:
            读取进程环境变量中的 API Key 与 ``QWEN_BASE_URL``；写一条结构化 build 日志。
        """

        configured_base = (
            model_settings.base_url if model_settings is not None else None
        ) or environ.get(_QWEN_BASE_URL_ENV)
        base_url = configured_base or _DEFAULT_QWEN_BASE_URL
        return build_openai_compatible_chat_model(
            model_name,
            model_settings,
            base_url=base_url,
            chat_model_class=QwenChatOpenAI,
            log_event="llm_qwen_build",
            log_label="Qwen",
        )
