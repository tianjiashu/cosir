"""按已解析的 ``ResolvedModel`` 构造 OpenAI SDK 模型适配器。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI
from openai.types.shared_params import FunctionDefinition

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.agents.resolved_model import ResolvedModel
from app.core.llm_provider.openai_model import OpenAIChatModel
from app.utils.http_proxy import build_proxy_async_client

__all__ = [
    "ResolvedChatModel",
    "build_chat_model",
    "resolve_chat_model",
]


@dataclass(frozen=True, slots=True)
class ResolvedChatModel:
    """一次 Run 解析后的模型及其能力事实。

    模型对象和能力来自同一个 ``ResolvedModel``，供 Workflow 同时使用，避免模型
    构建与思考通道解析各自读取配置而产生两套口径。该值对象不持有数据库 session，也不
    负责 Run 状态迁移或模型调用。
    """

    model: OpenAIChatModel
    model_name: str
    context_window_k: int
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_image: bool


def build_chat_model(
    settings: ResolvedModel, *, tools: Sequence[FunctionDefinition] = ()
) -> OpenAIChatModel:
    """构造 Run 独占的 OpenAI SDK client；厂商参数仅在此处适配。"""
    effort = settings.reasoning_effort if settings.supports_reasoning_effort else "max"
    extra: dict[str, Any] = {"thinking": {"type": "enabled"}, "reasoning_effort": effort}
    name = settings.model_name.lower()
    if "glm" in name:
        extra["thinking"]["clear_thinking"] = False
    if "minimax" in name:
        extra["reasoning_split"] = True
    parameters: dict[str, Any] = {
        "model": settings.model_name,
        "extra_body": extra,
        "temperature": 1 if "glm-5.3-flash" in name else settings.temperature,
        "top_p": 0.95 if "glm-5.3-flash" in name else settings.top_p,
        "seed": Constant.LLM.SEED,
    }
    if settings.max_tokens is not None:
        parameters["max_completion_tokens"] = settings.max_tokens
    client = AsyncOpenAI(
        api_key=settings.api_key or "local",
        base_url=settings.base_url or None,
        max_retries=Constant.LLM.MAX_RETRIES,
        timeout=Constant.LLM.REQUEST_TIMEOUT_SECONDS,
        http_client=build_proxy_async_client(
            base_url=settings.base_url or "", timeout=Constant.LLM.REQUEST_TIMEOUT_SECONDS
        ),
    )
    log.info(
        "llm_model_selected",
        extra={
            "msg": "使用 OpenAI SDK 构建模型",
            "data": {"model": settings.model_name, "base_url": settings.base_url},
        },
    )
    return OpenAIChatModel(client, parameters, tools=tools)


def resolve_chat_model(
    *,
    model: ResolvedModel,
    tools: Sequence[FunctionDefinition],
) -> ResolvedChatModel:
    """从本次 Run 已解析的完整模型构建模型对象及能力事实。

    参数:
        model: ``AgentRuntimeConfig.model``；解析时已保证连接与能力字段完整。
        tools: Task 固化的工具定义，保持顺序，不按当前 Run 禁用集合过滤。

    返回:
        已绑定模型连接配置的 ``ResolvedChatModel``，其中模型对象和能力字段来自同一个
        ``ResolvedModel``。

    异常:
        ValueError: 推理强度不被物化的模型能力支持。

    副作用:
        创建一个供本次 Run 使用的 HTTP client；不读取配置服务。
    """
    if model.reasoning_effort is not None and not model.supports_reasoning_effort:
        raise ValueError("reasoning_effort preference is not supported by the selected model")
    return ResolvedChatModel(
        model=build_chat_model(model, tools=tools),
        model_name=model.model_name,
        context_window_k=model.context_window_k,
        supports_thinking=model.supports_thinking,
        supports_reasoning_effort=model.supports_reasoning_effort,
        supports_image=model.supports_image,
    )
