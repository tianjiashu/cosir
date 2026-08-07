"""OpenAI 兼容 provider 的共享构建基座。

多个模型服务商（DeepSeek / Qwen 等）都以 ``langchain_openai.ChatOpenAI`` 接入其 OpenAI
兼容端点，构造过程高度一致。本模块收敛两类共享逻辑：
- ``OpenAICompatibleChatOpenAI``：补回 langchain-openai 在流式分块转换时丢弃的
  ``reasoning_content``（思考过程），为服务商无关的通用行为；
- ``build_openai_compatible_chat_model``：OpenAI 兼容 chat model 的构造样板（Key 解析、
  采样参数组装、结构化日志、构造），各 provider 只注入 base_url 与日志标识即可。

遵循《Agent 代码开发规范》「不重复造轮子」：把真实重复的 provider 构建机制上移为共享
实现，而不是逐个 provider 复制一份。
"""

from os import environ
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.config.logging.logger import log
from app.core.llm.model_http_pool import get_shared_model_http_client
from app.core.llm.model_settings import ModelSettings


class OpenAICompatibleChatOpenAI(ChatOpenAI):
    """补齐 langchain-openai 遗漏 ``reasoning_content`` 的 OpenAI 兼容 ``ChatOpenAI``。

    ``langchain_openai`` 的流式分块转换会丢弃 OpenAI 兼容端点（如 DeepSeek / Qwen）在 delta
    中返回的 ``reasoning_content``（思考过程），该字段既不进入 ``content`` 也不进入
    ``additional_kwargs``。本基类在父类转换后把该字段写入
    ``AIMessageChunk.additional_kwargs["reasoning_content"]``，供上层（workflow）提取并作为
    thinking 事件推送给客户端。无 thinking 内容时（如 flash 模型）该字段不写入，行为与普通
    ``ChatOpenAI`` 一致。服务商无关，各 provider 继承即可。
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


def build_openai_compatible_chat_model(
    model_name: str,
    model_settings: ModelSettings | None,
    base_url: str | None,
    chat_model_class: type[ChatOpenAI],
    log_event: str,
    log_label: str,
) -> BaseChatModel:
    """按 OpenAI 兼容端点构造 chat model 的共享样板。

    负责 API Key 解析、采样参数（thinking / temperature / top_p / max_tokens）组装、
    结构化构建日志与 ``chat_model_class`` 实例化；provider 只需注入各自的 base_url、
    模型类与日志标识，不再各自复制一遍构建逻辑。

    参数:
        model_name: 模型名称（如 ``deepseek-v4-flash`` / ``qwen3.7-plus``）。
        model_settings: 可选 Agent 级模型覆盖配置（采样参数）。某生成参数被显式提供时
            覆盖对应默认构造行为；未提供的参数不传入（沿用模型 / 客户端默认）。
        base_url: 已解析的 OpenAI 兼容端点 URL（由调用方决定回落链）；为 None 时
            沿用 ``ChatOpenAI`` 客户端默认端点（如 DeepSeek 未显式配置端点）。
        chat_model_class: 待实例化的 ``ChatOpenAI`` 子类（通常为 provider 自己的类）。
        log_event: 结构化日志事件名（如 ``llm_qwen_build``）。
        log_label: 日志中的人类可读服务商名（如 ``Qwen``）。

    返回:
        配置好 base_url / api_key / thinking / 采样参数的 ``chat_model_class`` 实例。

    异常:
        无（缺 API Key 时以 ``api_key=None`` 构造，由 factory 的统一回退兜底）。

    副作用:
        读取进程环境变量中的 API Key；写一条结构化 build 日志。
    """

    api_key_env = model_settings.api_key_env if model_settings is not None else None
    api_key = environ.get(api_key_env) if api_key_env else None
    extra: dict[str, Any] = {}
    if model_settings is not None:
        if model_settings.thinking:
            extra["thinking"] = {"type": "enabled"}
        if model_settings.temperature is not None:
            extra["temperature"] = model_settings.temperature
        if model_settings.top_p is not None:
            extra["top_p"] = model_settings.top_p
        if model_settings.max_tokens is not None:
            extra["max_tokens"] = model_settings.max_tokens
    log.info(
        log_event,
        extra={
            "msg": f"构建 {log_label} chat model，model={model_name}",
            "data": {"model": model_name, "base_url": base_url},
        },
    )
    # 注入进程级共享 AsyncClient：复用 TCP/TLS 连接，省去每 turn 重复握手；
    # ChatOpenAI 不会自动关闭传入的客户端，统一由 close_shared_model_http_clients 释放。
    shared_client = get_shared_model_http_client(base_url)
    return chat_model_class(
        model=model_name,
        base_url=base_url,
        api_key=SecretStr(api_key) if api_key else None,
        streaming=True,
        http_async_client=shared_client,
        **extra,
    )
