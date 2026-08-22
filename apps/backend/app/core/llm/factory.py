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

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_litellm import ChatLiteLLM

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.llm.model_settings import ModelSettings
from app.models import LLMRuntimeConfig
from app.models.provider_capability import ProviderCapability, get_capability
from app.service.depends import get_model_resolver_service

if TYPE_CHECKING:
    from app.service.llm.model_resolver_service import ModelResolverService

__all__ = [
    "ResolvedChatModel",
    "build_chat_model",
    "resolve_chat_model",
]

# 兜底请求超时（秒）：仅当 capability 未提供且 ModelSettings 也未覆盖时使用。
# 正常路径由 ``ProviderCapability.default_timeout_seconds`` 提供。
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class ResolvedChatModel:
    """``resolve_chat_model`` 的返回结果：已构建模型 + 本次解析的运行时配置。

    属性:
        model: 已构建的 LangChain ``BaseChatModel``（``ChatLiteLLM``）。
        config: 本次解析出的 ``LLMRuntimeConfig``（含 ``provider_type`` /
            ``thinking_channels`` 等，供 ``model_node``
            按厂商分派 thinking 抽取与剥离）。
    """

    model: BaseChatModel
    config: LLMRuntimeConfig


def _resolve_capability(
    model_name: str,
    model_settings: ModelSettings | None,
) -> ProviderCapability:
    """按厂商类型（或 model_name 前缀回退）解析能力元数据。

    优先取 ``model_settings.provider_type``（解析链透传，可区分 qianfan / xfyun
    等前缀与 openai-compatible 的差异）；缺省时按 ``model_name.split("/")[0]``
    前缀查注册表。未知类型经 ``get_capability`` 回退 custom 语义，不抛。

    参数:
        model_name: 模型名（带 provider 前缀）。
        model_settings: 可选的 Agent 级覆盖配置，可能携带 ``provider_type``。

    返回:
        匹配的 ``ProviderCapability``（未知类型回退 custom 语义）。

    异常:
        无。

    副作用:
        无。
    """

    provider_type = model_settings.provider_type if model_settings is not None else None
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
        model_name: str,
        model_settings: ModelSettings | None = None,
) -> BaseChatModel:
    """按模型名称构建 LangChain chat model（ChatLiteLLM 单一收口）。

    屏蔽不同模型的 ``thinking`` / 采样参数差异，返回的对象同时支持流式
    （``.astream()``）与非流式（``.ainvoke()``）调用。Agent 级覆盖配置
    （``model_settings``）若存在，会覆盖模型默认的对应构造项。

    按 ``ProviderCapability`` 透传接入参数（设计文档阶段 2）：
    - ``max_retries`` = ``capability.default_max_retries``（``ModelSettings``
      可覆盖）；``drop_params`` = ``capability.default_drop_params``（非严格
      兼容厂商 True，litellm 丢弃不支持参数避免 400）。
    - azure 接入参数透传（``provider_type`` / ``thinking_channels`` 等）。
    - thinking 注入仅在 ``capability.supports_thinking`` 且 ``config.thinking``
      时进行（``model_kwargs["thinking"] = {"type": "enabled"}``），并优先使用
      ``model_settings.thinking_request``（厂商专属开启参数，如 Anthropic
      budget_tokens）。
    - ``request_timeout`` = ``model_settings.timeout_seconds`` 覆盖，否则
      ``capability.default_timeout_seconds``。
    - 代理经 ``Settings.WEB_PROXY_*`` 注入（None 时不注入）。

    参数:
        model_name: 上层传入的模型名称（带 provider 前缀，如
            ``deepseek/deepseek-v4-flash``），原样透传给 litellm 路由。
        model_settings: 可选 Agent 级模型覆盖配置；其中 ``api_key`` 直接承载
            Key 明文（DB 唯一事实来源，缺失拦截在上游 resolver 完成），
            ``base_url`` 作为可选 api_base 覆盖，``thinking`` / ``temperature`` /
            ``top_p`` / ``max_tokens`` 作为采样与思考配置，``provider_type`` /
            ``thinking_channels`` / ``thinking_request`` /
            ``max_retries`` / ``drop_params`` / ``timeout_seconds`` 作为接入与
            thinking 生命周期覆盖。

    返回:
        一个 LangChain ``BaseChatModel``（``ChatLiteLLM``）实例（每次调用返回新实例，
        可安全复用）。

    异常:
        无（Key 缺失校验已上移至解析链 ``ModelResolverService``，本函数不抛缺 Key
        错误；不再读进程环境变量）。

    副作用:
        写一条结构化构建日志（``llm_model_selected``），data 记录
        ``model`` / ``api_key_provided`` / ``base_url`` / ``thinking`` /
        ``provider_type`` / ``drop_params`` / ``max_retries``，不输出 Key 明文。
    """

    api_key = model_settings.api_key if model_settings is not None else None

    base_url = model_settings.base_url if model_settings is not None else None
    temperature = model_settings.temperature if model_settings is not None else None
    top_p = model_settings.top_p if model_settings is not None else None
    max_tokens = model_settings.max_tokens if model_settings is not None else None
    thinking = model_settings.thinking if model_settings is not None else None
    thinking_request = model_settings.thinking_request if model_settings is not None else None
    max_retries = model_settings.max_retries if model_settings is not None else None
    drop_params = model_settings.drop_params if model_settings is not None else None
    timeout_seconds = model_settings.timeout_seconds if model_settings is not None else None

    capability = _resolve_capability(model_name, model_settings)

    model_kwargs: dict = {}
    if capability.supports_thinking and thinking:
        # 优先使用调用方预置的厂商专属开启参数（Anthropic budget_tokens 等）；
        # 缺省退化为通用 thinking 开关。
        model_kwargs["thinking"] = thinking_request or {"type": "enabled"}

    request_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else capability.default_timeout_seconds
        if capability.default_timeout_seconds > 0
        else _DEFAULT_REQUEST_TIMEOUT_SECONDS
    )
    effective_max_retries = (
        max_retries if max_retries is not None else capability.default_max_retries
    )
    effective_drop_params = (
        drop_params if drop_params is not None else capability.default_drop_params
    )

    http_client = _build_proxy_client()

    log.info(
        "llm_model_selected",
        extra={
            "msg": f"选用 ChatLiteLLM 构建模型，model={model_name}",
            "data": {
                "model": model_name,
                "api_key_provided": api_key is not None,
                "base_url": base_url,
                "thinking": thinking,
                "provider_type": capability.provider_type,
                "drop_params": effective_drop_params,
                "max_retries": effective_max_retries,
            },
        },
    )
    # model_name 原样透传（带 provider 前缀），路由/连接/缓存字段全交 litellm；
    # api_key 为 None 时 litellm 按前缀自动解析环境变量，请求期失败由 litellm 抛错。
    # drop_params/http_client 是 ChatLiteLLM 透传 litellm 的合法运行
    # 期参数，但其类型桩未声明（langchain_litellm 0.7.0），故忽略 call-arg 检查。
    # streaming=True 是「真正逐 token 流式」的硬开关：LangChain 的 astream 在
    # streaming=False 时会退化成 ainvoke + 单次 yield，导致每个模型 step 只产出 1 个
    # 整块 chunk，前端表现为「整段一次性出现、无流式感」。本工厂产出的模型由
    # model_node 统一经 astream 消费（见 core/workflows/nodes/model_node.py），
    # 故默认开启流式；非流式场景（如某些校验调用）仍可在调用方按需覆盖。
    return ChatLiteLLM(
        model=model_name,
        api_key=api_key,
        api_base=base_url,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        model_kwargs=model_kwargs,
        request_timeout=request_timeout,
        max_retries=effective_max_retries,
        drop_params=effective_drop_params,
        http_client=http_client,
        streaming=True,
    )  # type: ignore[call-arg]


def resolve_chat_model(
        *,
        requested_model: "str | None" = None,
        task_id: "str | None" = None,
        turn_id: "str | None" = None,
) -> "ResolvedChatModel":
    """运行期解析 ``LLMRuntimeConfig`` 并构建 chat model（设计 §6 ②）。

    经 ``ModelResolverService`` 解析 ``requested_model``（无 Auto 折叠语义：
    函数不再接收 ``default_model_name``，``requested_model`` 为 None 时原样传
    给 resolver 按 None 查询，命中失败即抛 ``ModelNotConfiguredError``），
    解析失败由 resolver 抛出（调用方在 workflow 运行期收敛为 RUN_FAILED）。
    解析成功后复用既有 ``build_chat_model`` 收口构建，保持 Key 读取 / 缺失校验
    的唯一出口不变，并把解析出的 ``LLMRuntimeConfig`` 一并返回供
    ``model_node`` 按厂商分派 thinking（``thinking_channels`` /
    ``thinking_roundtrip`` 等）。

    参数:
        requested_model: 可选，运行期显式请求的模型名（来自 turn 落库的
            ``model_name``，为 None 属防御兜底）；原样传给 resolver 查询。
        task_id: 可选，当前 task 标识（供日志上下文）。
        turn_id: 可选，当前 turn 标识（运行期解析模型名来源，供日志上下文）。

    返回:
        ``ResolvedChatModel``（含已构建的 ``BaseChatModel`` 与解析出的
        ``LLMRuntimeConfig``）。

    异常:
        ModelNotConfiguredError: 当解析链未命中启用模型 / 厂商禁用 / Key 未配置时
            由 resolver 抛出。

    副作用:
        经 resolver 打开主库只读 session（每次查询），拒绝时写 warn 级
        ``model_resolve_rejected`` 日志；经 ``build_chat_model`` 写
        ``llm_model_selected`` 构建日志（Key 缺失在 resolver 侧已拦截为
        ``ModelNotConfiguredError``）。
    """
    resolver: ModelResolverService = get_model_resolver_service()
    # 无default_model_name，直接解析 requested_model。
    config: LLMRuntimeConfig = resolver.resolve(
        requested_model=requested_model,
        log_context=_build_resolution_context(task_id, turn_id),
    )
    model = build_chat_model(
        config.model_name,
        model_settings=ModelSettings(
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            top_p=config.top_p,
            max_tokens=config.max_tokens,
            thinking=config.thinking,
            provider_type=config.provider_type,
            thinking_channels=config.thinking_channels,
            thinking_roundtrip=config.thinking_roundtrip,
            thinking_request=config.thinking_request,
            thinking_display=config.thinking_display,
        ),
    )
    return ResolvedChatModel(model=model, config=config)


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
