"""模型工厂：按模型名称构建 LangChain chat model（ChatOpenAI 单一收口）。

工厂经 ``langchain_openai.ChatOpenAI`` 实例化并透传采样参数与思考配置，对 DeepSeek / GLM /
qwen / kimi 等国内厂商的 OpenAI 兼容端点契合度较高。返回的模型对象天然
同时支持流式（``.astream()``）与非流式（``.ainvoke()``）调用——工厂本身只负责「按名取模型
+ 透传参数」，不掺入编排、工具绑定或事件翻译（留在 ``workflow`` / ``langchain_bridge``）。
缺 Key 拦截在构建期之前的解析链完成，构建期不读环境变量、不抛缺 Key 错误。
"""

from typing import Any

from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.model_settings import ModelSettings
from app.core.llm_provider.capability.capability_service import CapabilityService
from app.core.llm_provider.reasoning_chat_openai import ReasoningChatOpenAI
from app.models import ConversationRunRecord
from app.service.depends import get_model_config_service
from app.utils.http_proxy import build_proxy_async_client, build_proxy_client

__all__ = [
    "build_chat_model",
    "resolve_chat_model",
]

def build_chat_model(
    model_config_id: int,
    model_name: str,
    model_settings: ModelSettings,
) -> BaseChatModel:
    """按模型名构建 ``ChatOpenAI`` 实例（OpenAI-compatible 单一收口）。

    职责边界：只负责「按名取模型 + 透传采样/接入参数」，不掺入编排、工具绑定或事件翻译。
    ``model_name`` 来自模型连接配置；连接路由由配置行中的 ``base_url`` 与 ``api_key`` 决定，
    不再要求模型存在于厂商 JSON 目录。

    参数透传分层：
    - OpenAI 标准参数（``temperature`` / ``top_p`` / ``max_tokens`` / ``max_retries`` /
      ``request_timeout`` / ``reasoning_effort`` / ``seed``）作为 ``ChatOpenAI`` 顶层命名参数
      直接传入；``max_retries`` / ``request_timeout`` / ``seed`` 取 ``Constant.LLM``
      （所有模型一致，无按模型覆盖路径）。
    - 不猜测厂商私有参数，只发送 OpenAI-compatible 标准参数；
    - ``reasoning_effort`` 命中模型能力时与 ``temperature`` 互斥，强制不传 ``temperature``；
      内部档位（``low``/``high``/``max``）经 ``effort_map`` 翻译（见 ``_resolve_effort``），
      未命中映射则跳过注入并记录 warning，不抛错。

    参数:
        model_config_id: 模型连接配置标识。
        model_name: 模型名；必须与配置中的模型名一致。
        model_settings: Agent 级模型覆盖配置（采样参数 / 流式 / 推理强度）。

    返回:
        配置完成的 ``ChatOpenAI`` 实例（同时支持 ``.astream()`` 与 ``.ainvoke()``）。

    异常:
        ValueError: 配置不存在或模型名与配置不一致。

    副作用:
        创建供 OpenAI-compatible SDK 使用的 HTTP 客户端；写入一次 ``llm_model_selected``
        info 日志（不输出 api_key 明文）；
        ``api_key`` 以 ``SecretStr`` 封装传入（None 时透传 None，由端点决定鉴权）。
    """

    config = get_model_config_service().get_config(model_config_id)
    if model_name != config.model_name:
        raise ValueError("model_name must match the selected model configuration")

    api_key = config.api_key
    # ChatOpenAI 的 api_key 字段期望 SecretStr（避免明文在 repr/日志泄露）；None 时透传 None。
    chat_api_key: SecretStr | None = SecretStr(api_key) if api_key else None
    base_url = config.base_url

    resolved_effort = CapabilityService.resolve_reasoning_effort(
        model_name,
        model_settings.reasoning_effort,
    )

    model_kwargs: dict[str, Any] = {}
    if model_settings.max_tokens is not None:
        model_kwargs["max_completion_tokens"] = model_settings.max_tokens

    log.info(
        "llm_model_selected",
        extra={
            "msg": "选用 ChatOpenAI 构建模型",
            "data": {
                "model": model_name,
                "base_url": base_url,
                "api_key_provided": api_key is not None,
                "model_config_id": model_config_id,
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
        model=model_name,
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
    run: ConversationRunRecord | None = None,
    agent_profile: AgentProfile | None = None,
) -> BaseChatModel:
    """解析任务/轮次/智能体配置，返回 ``ChatOpenAI`` 实例。

    参数:
        run: 本次执行的 Conversation Run，提供模型连接配置和推理强度覆盖值。
        agent_profile: 本次执行的 Agent profile，提供未被 Run 覆盖的模型配置。

    返回:
        已绑定模型连接配置的 ``BaseChatModel``。

    异常:
        ValueError: profile、Run 或最终模型连接配置缺失，或配置中的模型名不一致。

    副作用:
        查询模型静态能力并创建一个供本次 Run 使用的 HTTP client；不修改共享 profile。

    以 ``agent_profile`` 的 ``model_name`` / ``model_config_id`` / ``model_settings`` 为默认；
    当 ``run`` 提供 ``model_name`` / ``model_config_id`` / ``reasoning_effort`` 时，运行时覆盖
    默认值（其余采样参数仍取 agent_profile）。最终委托 ``build_chat_model`` 构建。
    """
    if agent_profile is None:
        raise ValueError("agent_profile is required")
    if run is None:
        raise ValueError("run is required")
    model_settings: ModelSettings = agent_profile.model_settings
    model_name = run.model_name or agent_profile.model_name
    model_config_id = run.model_config_id or agent_profile.model_config_id
    if run.reasoning_effort is not None:
        model_settings.reasoning_effort = run.reasoning_effort

    if model_config_id is None:
        raise ValueError("model_config_id must be configured")
    model_name = get_model_config_service().get_config(model_config_id).model_name
    return build_chat_model(model_config_id, model_name, model_settings=model_settings)
