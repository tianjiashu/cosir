"""DeepSeek 模型提供商——基于 LangChain OpenAI 兼容客户端构建 chat model。

单一职责：把 DeepSeek 的模型规格（base_url / api_key / thinking 差异）封装为
``langchain_openai.ChatOpenAI`` 实例。所有流式解析、工具调用累积、消息格式转换都交给
LangChain + ``core/llm/langchain_bridge`` + LangGraph，本类不重复实现。

职责边界：
- 负责：DeepSeek 特有的 chat model 构造（含 thinking 模式注入、复用共享构建基座）。
- 不负责：消息/工具 schema 转换（langchain_bridge）、流式 token 解析（LangChain +
  LangGraph astream）、缺 Key 回退（factory 统一处理）、工具绑定（workflow 中 bind_tools）、
  共享 HTTP 连接池管理（委托 ``app.core.llm.model_http_pool``）。
"""

from langchain_core.language_models import BaseChatModel

from app.config.logging.logger import log
from app.core.llm.llm_provider.base import LLMProvider
from app.core.llm.llm_provider.openai_compatible import (
    OpenAICompatibleChatOpenAI,
    build_openai_compatible_chat_model,
)
from app.core.llm.model_settings import ModelSettings


class DeepSeekChatOpenAI(OpenAICompatibleChatOpenAI):
    """DeepSeek 兼容的 ``ChatOpenAI``：补齐 langchain-openai 遗漏的 ``reasoning_content``。

    直接复用 ``OpenAICompatibleChatOpenAI`` 已实现的 ``reasoning_content`` 收集逻辑
    （与 Qwen 等 OpenAI 兼容端点行为一致），本类仅作为 DeepSeek 的明确类型标识，
    不重复实现任何转换逻辑。
    """


class DeepSeekProvider(LLMProvider):
    """DeepSeek 模型适配器，基于 LangChain OpenAI 兼容客户端。

    通过 ``DeepSeekChatOpenAI``（``OpenAICompatibleChatOpenAI`` 子类）接入 DeepSeek 的
    OpenAI 兼容端点；复用共享构建基座 ``build_openai_compatible_chat_model``（含 Key 解析、
    采样参数组装、共享 HTTP 连接池注入），返回的模型对象由 LangChain/LangGraph 负责流式输出、
    工具调用累积与 thinking 内容处理，本类只做构造。
    """

    def build(self, model_name: str, model_settings: ModelSettings | None = None) -> BaseChatModel:
        """构建面向 DeepSeek 的 LangChain chat model。

        参数:
            model_name: 模型名称，例如 ``deepseek-v4-flash``。
            model_settings: 可选 Agent 级模型覆盖配置（采样参数）。当其中某个生成参数
                被显式提供时，覆盖对应默认构造行为；未提供的参数不传入（沿用模型/客户端默认）。

        返回:
            配置好 base_url / api_key / thinking / 采样参数的 ``DeepSeekChatOpenAI`` 实例。
        """

        base_url = model_settings.base_url if model_settings is not None else None
        thinking = model_settings.thinking if model_settings is not None else None
        log.info(
            "llm_deepseek_build",
            extra={
                "msg": f"构建 DeepSeek chat model，model={model_name}",
                "data": {"model": model_name, "thinking": thinking},
            },
        )
        return build_openai_compatible_chat_model(
            model_name,
            model_settings,
            base_url=base_url,
            chat_model_class=DeepSeekChatOpenAI,
            log_event="llm_deepseek_build",
            log_label="DeepSeek",
        )
