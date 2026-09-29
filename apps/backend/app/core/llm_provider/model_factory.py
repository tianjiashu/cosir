"""模型工厂：按模型名称构建 LangChain chat model（ChatOpenAI 单一收口）。

工厂经 ``langchain_openai.ChatOpenAI`` 实例化并透传采样参数与思考配置，对 DeepSeek / GLM /
qwen / kimi 等国内厂商的 OpenAI 兼容端点契合度较高。返回的模型对象天然
同时支持流式（``.astream()``）与非流式（``.ainvoke()``）调用——工厂本身只负责「按名取模型
+ 透传参数」，不掺入编排、工具绑定或事件翻译（留在 ``workflow`` / ``langchain_bridge``）。
缺 Key 拦截在构建期之前的解析链完成，构建期不读环境变量、不抛缺 Key 错误。
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.model_settings import ModelSettings
from app.core.llm_provider.reasoning_chat_openai import ReasoningChatOpenAI
from app.utils.http_proxy import build_proxy_async_client, build_proxy_client

__all__ = [
    "ResolvedChatModel",
    "build_chat_model",
    "resolve_chat_model",
]


@dataclass(frozen=True, slots=True)
class ResolvedChatModel:
    """一次 Run 解析后的模型及其能力事实。

    模型对象和能力来自同一个已物化 ``ModelSettings``，供 Workflow 同时使用，避免模型
    构建与思考通道解析各自读取配置而产生两套口径。该值对象不持有数据库 session，也不
    负责 Run 状态迁移或模型调用。
    """

    model: BaseChatModel
    model_name: str
    context_window_k: int
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_image: bool

def build_chat_model(
    model_settings: ModelSettings,
) -> BaseChatModel:
    """使用已物化的模型运行设置构建模型。

    参数:
        model_settings: 同时包含连接字段、能力字段和用户覆盖项的运行配置。

    返回:
        已配置 HTTP 客户端和请求参数的聊天模型实例。

    异常:
        ModelSettingsError: 运行设置没有完成物化。

    副作用:
        创建模型使用的同步和异步 HTTP 客户端；不读取配置数据库。
    """

    model_settings.require_runtime_config()

    api_key = model_settings.api_key
    # ChatOpenAI 的 api_key 字段期望 SecretStr（避免明文在 repr/日志泄露）；None 时透传 None。
    chat_api_key: SecretStr | None = SecretStr(api_key) if api_key else None
    base_url = model_settings.base_url




    resolved_effort = model_settings.reasoning_effort if model_settings.supports_reasoning_effort else None

    model_kwargs: dict[str, Any] = {}
    if model_settings.max_tokens is not None:
        model_kwargs["max_completion_tokens"] = model_settings.max_tokens

    log.info(
        "llm_model_selected",
        extra={
            "msg": "选用 ChatOpenAI 构建模型",
            "data": {
                "model": model_settings.model_name,
                "base_url": base_url,
                "api_key_provided": api_key is not None,
            },
        },
    )

    # 显式提供客户端，绕过 langchain-openai 在 Windows 上按 socket options 构造
    # ``request=`` transport 的兼容性问题（httpx 0.28 已移除该 Client 参数）。
    # 通过 ``build_proxy_client`` / ``build_proxy_async_client`` 把系统代理注入客户端：
    # ``CODING_AGENT_PROXY_AUTO_DETECT`` 关闭或无系统代理时返回 None，等价于不设置代理。
    # 代理在每次构建 client 时解析一次，因此系统代理开关变化会在下次解析时实时生效；不再于
    # 进程启动期全局写入环境变量。
    resolved_base_url = base_url or ""
    http_client = build_proxy_client(
        base_url=resolved_base_url, timeout=Constant.LLM.REQUEST_TIMEOUT_SECONDS
    )
    http_async_client = build_proxy_async_client(
        base_url=resolved_base_url, timeout=Constant.LLM.REQUEST_TIMEOUT_SECONDS
    )

    return ReasoningChatOpenAI(
        model=model_settings.model_name,
        # api_key 以 SecretStr 封装传入（langchain 推荐做法，防止明文在 repr/日志泄露）；
        # 包装逻辑见上方 ``chat_api_key`` 构造（None 时直接透传 None）。
        api_key=chat_api_key,
        base_url=base_url,
        # None 表示未覆盖，沿用系统默认的流式行为；False 才是显式关闭。
        streaming=True,
        http_client=http_client,
        http_async_client=http_async_client,
        stream_usage=True,
        max_retries=Constant.LLM.MAX_RETRIES,
        timeout=Constant.LLM.REQUEST_TIMEOUT_SECONDS,
        seed=Constant.LLM.SEED,
        temperature=model_settings.temperature,
        top_p=model_settings.top_p if model_settings.top_p is not None else None,
        # max_tokens=model_settings.max_tokens if model_settings.max_tokens is not None else None,
        reasoning_effort=resolved_effort,
        model_kwargs=model_kwargs or {},
        stream_options={"include_usage": True}
    )


def resolve_chat_model(
    *,
    agent_profile: AgentProfile,
) -> ResolvedChatModel:
    """从 Agent profile 的已物化 ModelSettings 构建模型及能力事实。

    参数:
        agent_profile: 本次执行的 per-Run Agent profile；其 ``model_settings`` 必须已包含
            模型连接字段和能力字段。

    返回:
        已绑定模型连接配置的 ``ResolvedChatModel``，其中模型对象和能力字段来自同一个
        ``ModelSettings``。

    异常:
        ValueError: profile 未提供或推理强度不被物化的模型能力支持。

    副作用:
        创建一个供本次 Run 使用的 HTTP client；不修改共享 profile，也不读取配置服务。
    """
    model_settings = agent_profile.model_settings.require_runtime_config()
    if (
        model_settings.reasoning_effort is not None
        and not model_settings.supports_reasoning_effort
    ):
        raise ValueError(
            "reasoning_effort preference is not supported by the selected model"
        )
    return ResolvedChatModel(
        model=build_chat_model(model_settings),
        model_name=model_settings.model_name,
        context_window_k=model_settings.context_window_k,
        supports_thinking=model_settings.supports_thinking,
        supports_reasoning_effort=model_settings.supports_reasoning_effort,
        supports_image=model_settings.supports_image,
    )
