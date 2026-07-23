"""DeepSeek 模型提供商——基于 LangChain OpenAI 兼容客户端构建 chat model。

单一职责：把 DeepSeek 的模型规格（base_url / api_key / thinking 差异）封装为
``langchain_openai.ChatOpenAI`` 实例。所有流式解析、工具调用累积、消息格式转换都交给
LangChain + ``core/llm/langchain_bridge`` + LangGraph，本类不重复实现。

职责边界：
- 负责：DeepSeek 特有的 chat model 构造（含 thinking 模式注入）。
- 不负责：消息/工具 schema 转换（langchain_bridge）、流式 token 解析（LangChain +
  LangGraph astream）、缺 Key 回退（factory 统一处理）、工具绑定（workflow 中 bind_tools）。
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.config.logging.logger import log
from app.core.llm.llm_provider.base import LLMProvider
from app.core.llm.model_settings import ModelSettings


class DeepSeekChatOpenAI(ChatOpenAI):
    """DeepSeek 兼容的 ``ChatOpenAI``：补齐 langchain-openai 遗漏的 ``reasoning_content``。

    ``langchain_openai`` 的 ``_convert_delta_to_message_chunk`` 会丢弃 OpenAI 兼容端点
    （如 DeepSeek）在 delta 中返回的 ``reasoning_content``（思考过程），该字段既不进入
    ``content`` 也不进入 ``additional_kwargs``。本子类在流式分块转换后把该字段写入
    ``AIMessageChunk.additional_kwargs["reasoning_content"]``，供上层（workflow）提取并作为
    thinking 事件推送给客户端。无 thinking 内容时（如 flash 模型）该字段不写入，行为与普通
    ``ChatOpenAI`` 一致。
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: Any,
        base_generation_info: dict[str, Any] | None,
    ) -> Any:
        """在父类转换后把 delta 中的 ``reasoning_content`` 收进 additional_kwargs。"""

        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk is None:
            return generation_chunk
        message = generation_chunk.message
        if not isinstance(message, AIMessageChunk):
            return generation_chunk
        choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices", [])
        if not choices:
            return generation_chunk
        delta = choices[0].get("delta") or {}
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk


class DeepSeekProvider(LLMProvider):
    """DeepSeek 模型适配器，基于 LangChain OpenAI 兼容客户端。

    通过 ``DeepSeekChatOpenAI``（``ChatOpenAI`` 子类）接入 DeepSeek 的 OpenAI 兼容端点；返回的
    模型对象由 LangChain/LangGraph 负责流式输出、工具调用累积与 thinking 内容处理，本类只做构造。
    """

    def build(self, model_name: str, model_settings: ModelSettings | None = None) -> BaseChatModel:
        """构建面向 DeepSeek 的 LangChain chat model。

        参数:
            spec: DeepSeek 模型规格（含 base_url / api_key_env / thinking）。
            model_settings: 可选 Agent 级模型覆盖配置（采样参数）。当其中某个生成参数
                被显式提供时，覆盖对应默认构造行为；未提供的参数不传入（沿用模型/客户端默认）。

        返回:
            配置好 base_url / api_key / thinking / 采样参数的 ``DeepSeekChatOpenAI`` 实例。
        """

        api_key = model_settings.api_key_env if model_settings is not None else None
        base_url = model_settings.base_url if model_settings is not None else None
        extra: dict = {}
        if model_settings is not None:
            if model_settings.thinking:
                extra["thinking"] = {"type": "enabled"}
            if model_settings.temperature is not None:
                extra["temperature"] = model_settings.temperature
            if model_settings.top_p is not None:
                extra["top_p"] = model_settings.top_p
            if model_settings.max_tokens is not None:
                extra["max_tokens"] = model_settings.max_tokens
        thinking = model_settings.thinking if model_settings is not None else None
        log.info(
            "llm_deepseek_build",
            extra={
                "msg": f"构建 DeepSeek chat model，model={model_name}",
                "data": {"model": model_name, "thinking": thinking},
            },
        )
        return DeepSeekChatOpenAI(
            model=model_name,
            base_url=base_url,
            api_key=SecretStr(api_key) if api_key else None,
            streaming=True,
            **extra,
        )
