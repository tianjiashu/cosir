"""模型工厂：按模型名称构建 LangChain chat model（ChatLiteLLM 单一收口）。

上层只需传入模型名称（如 ``deepseek/deepseek-v4-flash``），工厂直接实例化
``langchain_litellm.ChatLiteLLM`` 并透传采样参数与思考配置；模型路由 / 连接 /
``reasoning_content`` / ``usage_metadata`` 全部交由 litellm 处理。返回的模型对象天然
同时支持流式（``.astream()``）与非流式（``.ainvoke()``）调用，由调用方决定输入输出的
流/非流形态——工厂本身只负责「按名取模型」，不掺入编排、工具绑定或事件翻译（那些留在
``workflow`` / ``langchain_bridge``）。

缺 Key 处理：
- 解析链（``ModelResolverService`` → ``provider_service.api_key_configured``）在
  构建期之前完成 Key 缺失拦截，未命中启用模型 / 厂商禁用 / Key 未配置时抛
  ``ModelNotConfiguredError``（工作流运行期收敛为 RUN_FAILED）；
- 构建期本身不再读环境变量、不抛缺 Key 错误：``api_key`` 直接来自解析链透传
  的 ``LLMRuntimeConfig.api_key``（DB 唯一事实来源），为 None 时交 litellm 按
  模型前缀自动解析，请求期失败由 litellm 抛错。
"""

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_litellm import ChatLiteLLM

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.model_settings import ModelSettings
from app.llm_provider.provider.provider_capability import ProviderCapability, get_capability
from app.models import TurnRecord, TaskRecord
from app.service.depends import get_provider_service, get_model_entry_service


__all__ = [
    "build_chat_model",
    "resolve_chat_model",
]

# 兜底请求超时（秒）：仅当 capability 未提供且 ModelSettings 也未覆盖时使用。
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0

_DEFAULT_MAX_RETRIES = 3


def _resolve_capability(
        model_name: str,
        runtime_config: "LLMRuntimeConfig | None",
) -> ProviderCapability:
    """按厂商类型（或 model_name 前缀回退）解析能力元数据。

    优先取 ``runtime_config.provider_type``（解析链透传，可区分 qianfan / xfyun
    等前缀与 openai-compatible 的差异）；缺省时按 ``model_name.split("/")[0]``
    前缀查注册表。未知类型经 ``get_capability`` 回退 custom 语义，不抛。

    参数:
        model_name: 模型名（带 provider 前缀）。
        runtime_config: 可选 ``LLMRuntimeConfig``，可能携带 ``provider_type``。

    返回:
        匹配的 ``ProviderCapability``（未知类型回退 custom 语义）。

    异常:
        无。

    副作用:
        无。
    """

    provider_type = runtime_config.provider_type if runtime_config is not None else None
    if provider_type:
        return get_capability(provider_type)
    prefix = model_name.split("/", 1)[0]
    return get_capability(prefix)


def _build_proxy_client() -> httpx.AsyncClient | None:
    """按 ``Settings.WEB_PROXY_*`` 构造可复用的异步 HTTP 客户端代理。

    仅当 ``WEB_PROXY_URL`` 非空时构造；代理认证（``WEB_PROXY_API_KEY``）以
    ``Authorization: Bearer`` 头注入。None 时不注入代理，保持既有无代理链路。

    参数:
        无。

    返回:
        配好代理的 ``httpx.AsyncClient``；未配置代理时返回 None。

    异常:
        无（httpx 客户端构造不进行网络 I/O，不抛）。

    副作用:
        无（客户端构造是纯内存对象创建）。
    """

    proxy_url = Settings.WEB_PROXY_URL
    if not proxy_url:
        return None
    headers: dict[str, str] = {}
    if Settings.WEB_PROXY_API_KEY:
        headers["Authorization"] = f"Bearer {Settings.WEB_PROXY_API_KEY}"
    return httpx.AsyncClient(
        proxy=proxy_url,
        headers=headers,
        timeout=Settings.WEB_REQUEST_TIMEOUT_SECONDS,
    )


def build_chat_model(
        product_id: str,
        model_id: str,
        model_settings: ModelSettings,
) -> BaseChatModel:

    provider = get_provider_service().get_provider(product_id)
    model_entry = get_model_entry_service().get_model(model_id)

    api_key = provider.api_key
    base_url = provider.base_url
    temperature = model_settings.temperature
    top_p = model_settings.top_p
    max_tokens = model_settings.max_tokens
    thinking = model_settings.thinking

    model_kwargs: dict = {}
    if model_entry.supports_thinking:
        # 优先使用调用方预置的厂商专属开启参数（Anthropic budget_tokens 等）；
        if thinking:
            reasoning_effort = model_settings.reasoning_effort or "max"
            model_kwargs["thinking"] = {"type": "enabled", "reasoning_effort": reasoning_effort}
        else:
            model_kwargs["thinking"] = {"type": "disabled"}

    if model_settings.response_format:#json_object
        model_kwargs["response_format"] = model_settings.response_format



    request_timeout = (
        model_settings.timeout_seconds
        if model_settings.timeout_seconds > 0
        else _DEFAULT_REQUEST_TIMEOUT_SECONDS
    )

    effective_max_retries = (
        model_settings.max_retries
        if model_settings.max_retries > 0
        else _DEFAULT_MAX_RETRIES
    )

    streaming = model_settings.stream or True

    http_client = _build_proxy_client()

    log.info(
        "llm_model_selected",
        extra={
            "msg": f"选用 ChatLiteLLM 构建模型，model={model_entry.model_name}",
            "data": {
                "model": model_entry.model_name,
                "api_key_provided": api_key is not None,
                "base_url": base_url,
                "thinking": thinking,
                "provider_type": provider.name,
                "max_retries": effective_max_retries,
            },
        },
    )

    return ChatLiteLLM(
        model=model_entry.model_name,
        api_key=api_key,
        api_base=base_url,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        model_kwargs=model_kwargs,
        request_timeout=request_timeout,
        max_retries=effective_max_retries,
        streaming=streaming,
    )  # type: ignore[call-arg]


def resolve_chat_model(
        *,
        task: TaskRecord | None = None,
        turn: TurnRecord | None = None,
        agent_profile: AgentProfile | None = None,
) -> BaseChatModel:
    """
        根据任务、轮次、智能体配置，解析并返回一个 ChatLiteLLM 模型实例。
        ModelSettings 为默认模型参数配置
        turn内的为运行时模型参数配置
    """
    model_settings: ModelSettings = agent_profile.model_settings
    model_id = agent_profile.model_id
    product_id = agent_profile.product_id
    if turn.model_id is not None:
        model_id = turn.model_id
    if turn.product_id is not None:
        product_id = turn.product_id
    if turn.thinking is not None:
        model_settings.thinking = turn.thinking
    if turn.reasoning_effort is not None:
        model_settings.reasoning_effort = turn.reasoning_effort

    return build_chat_model(product_id, model_id, model_settings=model_settings)


def _build_resolution_context(
        task_id: str | None,
        turn_id: str | None,
) -> dict[str, object]:
    """组装运行期解析日志上下文（task/turn 标识）。

    参数:
        task_id: 可选 task 标识。
        turn_id: 可选 turn 标识。

    返回:
        含 ``task_id`` / ``turn_id`` 的上下文字典（缺省字段不包含）。

    异常:
        无。

    副作用:
        无。
    """

    ctx: dict[str, object] = {}
    if task_id is not None:
        ctx["task_id"] = task_id
    if turn_id is not None:
        ctx["turn_id"] = turn_id
    return ctx
