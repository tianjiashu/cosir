"""模型工厂：按模型名称构建 LangChain chat model（ChatOpenAI 单一收口）。

工厂经 ``langchain_openai.ChatOpenAI`` 实例化并透传采样参数与思考配置，对 DeepSeek / GLM /
qwen / kimi 等国内厂商的 OpenAI 兼容端点契合度优于 litellm 多厂商路由。返回的模型对象天然
同时支持流式（``.astream()``）与非流式（``.ainvoke()``）调用——工厂本身只负责「按名取模型
+ 透传参数」，不掺入编排、工具绑定或事件翻译（留在 ``workflow`` / ``langchain_bridge``）。
缺 Key 拦截在构建期之前的解析链完成，构建期不读环境变量、不抛缺 Key 错误。
"""

from dataclasses import replace
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.model_settings import ModelSettings
from app.core.llm_provider.capability.model_capability import (
    ReasoningEffortCapability,
)
from app.core.llm_provider.capability.provider_capability import (
    ProviderCapability,
)
from app.models import ConversationRunRecord
from app.service.depends import get_provider_service
from app.service.provider.capability_service import CapabilityService

__all__ = [
    "build_chat_model",
    "resolve_chat_model",
]


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
    provider_id: int,
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
        provider_id: provider 归属标识，用于查询接入配置（base_url / api_key）。
        model_name: 模型名（注册表/能力清单中的纯净模型名，不含厂商前缀）。
        model_settings: Agent 级模型覆盖配置（采样参数 / 流式 / 推理强度）。

    返回:
        配置完成的 ``ChatOpenAI`` 实例（同时支持 ``.astream()`` 与 ``.ainvoke()``）。

    异常:
        ValueError: ``model_name`` 不在该 provider 能力清单内，或 ``provider.name`` 未注册。

    副作用:
        创建供 OpenAI-compatible SDK 使用的 HTTP 客户端；写入一次 ``llm_model_selected``
        info 日志（不输出 api_key 明文）；
        ``api_key`` 以 ``SecretStr`` 封装传入（None 时透传 None，由端点决定鉴权）。
    """

    provider = get_provider_service().get_provider(provider_id)
    provider_capability: ProviderCapability = ProviderCapability.get_capability(provider.name)

    if model_name not in provider_capability.models:
        raise ValueError(f"model_name: {model_name} not in provider_capability.models")

    api_key = provider.api_key
    # ChatOpenAI 的 api_key 字段期望 SecretStr（避免明文在 repr/日志泄露）；None 时透传 None。
    chat_api_key: SecretStr | None = SecretStr(api_key) if api_key else None
    base_url = provider.base_url or provider_capability.default_base_url

    resolved_effort = CapabilityService.resolve_reasoning_effort(
        model_name,
        model_settings.reasoning_effort,
    )

    # 生成长度上限：max_tokens（顶层字段）与 max_completion_tokens（经 model_kwargs）同时设置，
    # 由厂商 disabled_params 屏蔽其一决定生效项（如 {"max_tokens": None} 仅用新名）。
    model_kwargs: dict[str, Any] = {}
    if model_settings.max_tokens is not None:
        # 两者均设置，具体生效项由 provider_capability.disabled_params 决定
        model_kwargs["max_completion_tokens"] = model_settings.max_tokens

    log.info(
        "llm_model_selected",
        extra={
            "msg": "选用 ChatOpenAI 构建模型",
            "data": {
                "model": model_name,
                "base_url": base_url,
                "api_key_provided": api_key is not None,
                "provider_type": provider.name,
            },
        },
    )

    # 显式提供客户端，绕过 langchain-openai 在 Windows 上按 socket options 构造
    # ``request=`` transport 的兼容性问题（httpx 0.28 已移除该 Client 参数）。
    resolved_base_url = base_url or ""
    http_client = httpx.Client(
        base_url=resolved_base_url, timeout=Settings.LLM_REQUEST_TIMEOUT_SECONDS
    )
    http_async_client = httpx.AsyncClient(
        base_url=resolved_base_url, timeout=Settings.LLM_REQUEST_TIMEOUT_SECONDS
    )

    return ChatOpenAI(
        model=model_name,
        # api_key 以 SecretStr 封装传入（langchain 推荐做法，防止明文在 repr/日志泄露）；
        # 包装逻辑见上方 ``chat_api_key`` 构造（None 时直接透传 None）。
        api_key=chat_api_key,
        base_url=base_url,
        streaming=bool(model_settings.stream),
        http_client=http_client,
        http_async_client=http_async_client,
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
    run: ConversationRunRecord | None = None,
    agent_profile: AgentProfile | None = None,
) -> BaseChatModel:
    """解析任务/轮次/智能体配置，返回 ``ChatOpenAI`` 实例。

    参数:
        run: 本次执行的 Conversation Run，提供模型、provider 和推理强度覆盖值。
        agent_profile: 本次执行的 Agent profile，提供未被 Run 覆盖的模型配置。

    返回:
        已绑定 provider/model 配置的 ``BaseChatModel``。

    异常:
        ValueError: profile、Run 或最终 provider/model 配置缺失，或模型不在 provider 能力清单。

    副作用:
        查询 provider 能力并创建一个供本次 Run 使用的 HTTP client；不修改共享 profile。

    以 ``agent_profile`` 的 ``model_name`` / ``provider_id`` / ``model_settings`` 为默认；
    当 ``run`` 提供 ``model_name`` / ``provider_id`` / ``reasoning_effort`` 时，运行时覆盖
    默认值（其余采样参数仍取 agent_profile）。最终委托 ``build_chat_model`` 构建。
    """
    if agent_profile is None:
        raise ValueError("agent_profile is required")
    if run is None:
        raise ValueError("run is required")
    model_settings: ModelSettings = replace(agent_profile.model_settings)
    model_name = agent_profile.model_name
    provider_id = agent_profile.provider_id
    if run.model_name is not None:
        model_name = run.model_name
    if run.provider_id is not None:
        provider_id = run.provider_id
    if run.reasoning_effort is not None:
        model_settings.reasoning_effort = run.reasoning_effort

    if provider_id is None or model_name is None:
        raise ValueError("provider_id and model_name must be configured")
    return build_chat_model(provider_id, model_name, model_settings=model_settings)
