"""模型工厂：按模型名称构建 LangChain chat model（ChatOpenAI 单一收口）。

工厂经 ``langchain_openai.ChatOpenAI`` 实例化并透传采样参数与思考配置，对 DeepSeek / GLM /
qwen / kimi 等国内厂商的 OpenAI 兼容端点契合度优于 litellm 多厂商路由。返回的模型对象天然
同时支持流式（``.astream()``）与非流式（``.ainvoke()``）调用——工厂本身只负责「按名取模型
+ 透传参数」，不掺入编排、工具绑定或事件翻译（留在 ``workflow`` / ``langchain_bridge``）。
缺 Key 拦截在构建期之前的解析链完成，构建期不读环境变量、不抛缺 Key 错误。
"""

from typing import Any

from pydantic import SecretStr
import httpx
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.model_settings import ModelSettings
from app.llm_provider.capability.model_capability import (
    ModelCapability,
    ReasoningEffortCapability,
)
from app.llm_provider.capability.provider_capability import (
    ProviderCapability,
)
from app.models import TaskRecord, TurnRecord
from app.service.depends import get_provider_service

__all__ = [
    "build_chat_model",
    "resolve_chat_model",
]


def _build_proxy_client() -> httpx.AsyncClient | None:
    """按 ``Settings.WEB_PROXY_*`` 构造可复用的异步 HTTP 客户端代理。

    仅当 ``WEB_PROXY_URL`` 非空时构造；代理认证（``WEB_PROXY_API_KEY``）以
    ``Authorization: Bearer`` 头注入。未配置代理时返回 None，保持既有无代理链路。
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


def _resolve_effort(
        model_name: str,
        requested_effort: str,
        effort_cap: ReasoningEffortCapability,
) -> str | None:
    """把内部推理强度档位翻译成厂商原始档位。

    内部档位（``low`` / ``high`` / ``max``）需经模型 ``effort_map`` 翻译成厂商原始档位
    （不同厂商档位取值不同，如 ``qwen-3.8-max`` 的 ``high``->``medium``、``max``->``xhigh``）
    再透传。未命中映射（模型声明支持推理但不支持该具体档位）返回 None，由调用方跳过注入。

    参数:
        model_name: 模型名（仅用于日志上下文，便于排查未命中）。
        requested_effort: 调用方请求的内部档位（已确认非 None）。
        effort_cap: 该模型的推理强度能力元数据（含 effort_map）。

    返回:
        翻译后的厂商原始档位字符串；未命中映射时返回 None。
    """
    resolved = effort_cap.effort_map.get(requested_effort)
    if resolved is None:
        log.warning(
            "llm_reasoning_effort_unmapped",
            extra={
                "msg": "推理强度档位无对应厂商映射，跳过注入",
                "data": {
                    "model": model_name,
                    "requested_effort": requested_effort,
                },
            },
        )
    return resolved


def build_chat_model(
        product_id: int,
        model_name: str,
        model_settings: ModelSettings,
) -> BaseChatModel:
    """按模型名构建 ``ChatOpenAI`` 实例（OpenAI-compatible 单一收口）。

    职责边界：只负责「按名取模型 + 透传采样/接入参数」，不掺入编排、工具绑定或事件翻译。
    ``model_name`` 为注册表/能力清单中的纯净模型名（如 ``deepseek-v4-flash``，不含厂商前缀）；
    厂商路由由 provider 行与 ``base_url`` 处理（空时取 ``ProviderCapability.default_base_url``）；
    ``provider.name`` 未在 ``llm_provider.json`` 注册时 ``get_capability`` 抛 ``ValueError``。

    参数透传分层：
    - OpenAI 标准参数（``temperature`` / ``top_p`` / ``max_tokens`` / ``max_retries`` /
      ``request_timeout`` / ``reasoning_effort`` / ``seed``）作为 ``ChatOpenAI`` 顶层命名参数
      直接传入；``max_retries`` / ``request_timeout`` 取全局 ``Settings``（所有模型一致）。
    - 厂商私有参数（如 vLLM ``use_beam_search``）经 ``ProviderCapability.extra_body`` 透传；
    - 厂商不兼容参数经 ``ProviderCapability.disabled_params`` 软屏蔽（如老模型不支持
      ``parallel_tool_calls`` / ``strict``）；
    - 生成长度上限 ``max_tokens`` 与 ``max_completion_tokens`` 同时设置，由 ``disabled_params``
      屏蔽其一决定生效项（详见下方实现注释）；
    - ``reasoning_effort`` 命中模型能力时与 ``temperature`` 互斥，强制不传 ``temperature``；
      内部档位（``low``/``high``/``max``）经 ``effort_map`` 翻译（见 ``_resolve_effort``），
      未命中映射则跳过注入并记录 warning，不抛错。

    参数:
        product_id: provider 归属标识，用于查询接入配置（base_url / api_key）。
        model_name: 模型名（注册表/能力清单中的纯净模型名，不含厂商前缀）。
        model_settings: Agent 级模型覆盖配置（采样参数 / 流式 / 推理强度）。

    返回:
        配置完成的 ``ChatOpenAI`` 实例（同时支持 ``.astream()`` 与 ``.ainvoke()``）。

    异常:
        ValueError: ``model_name`` 不在该 provider 能力清单内，或 ``provider.name`` 未注册。

    副作用:
        可能创建可复用的 ``httpx.AsyncClient`` 代理（仅当 ``WEB_PROXY_URL`` 配置）；
        写入一次 ``llm_model_selected`` info 日志（不输出 api_key 明文）；
        ``api_key`` 以 ``SecretStr`` 封装传入（None 时透传 None，由端点决定鉴权）。
    """

    provider = get_provider_service().get_provider(product_id)
    provider_capability:ProviderCapability = ProviderCapability.get_capability(provider.name)

    if model_name not in provider_capability.models:
        raise ValueError(
            f"model_name: {model_name} not in provider_capability.models"
        )
    model_capability = ModelCapability.get_capability(model_name)

    api_key = provider.api_key
    # ChatOpenAI 的 api_key 字段期望 SecretStr（避免明文在 repr/日志泄露）；None 时透传 None。
    chat_api_key: SecretStr | None = SecretStr(api_key) if api_key else None
    base_url = provider.base_url or provider_capability.default_base_url

    # 推理强度与 temperature 互斥：命中推理能力时强制不传 temperature。
    requested_effort = model_settings.reasoning_effort
    effort_cap = model_capability.reasoning_effort
    use_reasoning_effort = requested_effort is not None and effort_cap.supported
    # use_reasoning_effort 为 True 已蕴含 requested_effort 非 None，断言供 mypy 收窄类型。
    resolved_effort = None
    if use_reasoning_effort:
        assert requested_effort is not None
        resolved_effort = _resolve_effort(model_name, requested_effort, effort_cap)

    # 生成长度上限：max_tokens（顶层字段）与 max_completion_tokens（经 model_kwargs）同时设置，
    # 由厂商 disabled_params 屏蔽其一决定生效项（如 {"max_tokens": None} 仅用新名）。
    model_kwargs: dict[str, Any] = {}
    if model_settings.max_tokens is not None:
        # 两者均设置，具体生效项由 provider_capability.disabled_params 决定
        model_kwargs["max_completion_tokens"] = model_settings.max_tokens


    http_client = _build_proxy_client()

    log.info(
        "llm_model_selected",
        extra={
            "msg": "选用 ChatOpenAI 构建模型",
            "data": {
                "model": model_name,
                "base_url": base_url,
                "api_key_provided": api_key is not None,
                "provider_type": provider.name
            },
        },
    )


    return ChatOpenAI(
        model=model_name,
        # api_key 以 SecretStr 封装传入（langchain 推荐做法，防止明文在 repr/日志泄露）；
        # 包装逻辑见上方 ``chat_api_key`` 构造（None 时直接透传 None）。
        api_key=chat_api_key,
        base_url=base_url,
        streaming=bool(model_settings.stream),
        http_client=http_client,
        stream_usage=True,
        max_retries=Settings.LLM_MAX_RETRIES,
        timeout=Settings.LLM_REQUEST_TIMEOUT_SECONDS,
        extra_body=provider_capability.extra_body or None,
        disabled_params=provider_capability.disabled_params or None,
        seed=Settings.LLM_SEED,
        temperature=model_settings.temperature,
        top_p=model_settings.top_p if model_settings.top_p is not None else None,
        # max_tokens=model_settings.max_tokens if model_settings.max_tokens is not None else None,
        reasoning_effort=resolved_effort,
        model_kwargs=model_kwargs or {},
    )


def resolve_chat_model(
        *,
        task: TaskRecord | None = None,
        turn: TurnRecord | None = None,
        agent_profile: AgentProfile | None = None,
) -> BaseChatModel:
    """解析任务/轮次/智能体配置，返回 ``ChatOpenAI`` 实例。

    以 ``agent_profile`` 的 ``model_name`` / ``product_id`` / ``model_settings`` 为默认；
    当 ``turn`` 提供 ``model_name`` / ``product_id`` / ``reasoning_effort`` 时，运行时覆盖
    默认值（其余采样参数仍取 agent_profile）。最终委托 ``build_chat_model`` 构建。
    """
    model_settings: ModelSettings = agent_profile.model_settings
    model_name = agent_profile.model_name
    product_id = agent_profile.product_id
    if turn.model_name is not None:
        model_name = turn.model_name
    if turn.product_id is not None:
        product_id = turn.product_id
    if turn.reasoning_effort is not None:
        model_settings.reasoning_effort = turn.reasoning_effort

    return build_chat_model(product_id, model_name, model_settings=model_settings)
