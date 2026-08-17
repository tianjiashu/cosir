"""模型工厂：按模型名称构建 LangChain chat model（ChatLiteLLM 单一收口）。

上层只需传入模型名称（如 ``deepseek/deepseek-v4-flash``），工厂直接实例化
``langchain_litellm.ChatLiteLLM`` 并透传采样参数与思考配置；模型路由 / 连接 /
``reasoning_content`` / ``usage_metadata`` 全部交由 litellm 处理。返回的模型对象天然
同时支持流式（``.astream()``）与非流式（``.ainvoke()``）调用，由调用方决定输入输出的
流/非流形态——工厂本身只负责「按名取模型」，不掺入编排、工具绑定或事件翻译（那些留在
``workflow`` / ``langchain_bridge``）。

缺 Key 处理（两层）：
- ``api_key_env`` 显式配置但环境变量缺失（或为空）→ 构建期抛 ``ValueError``（不回退
  fake 模型），使配置错误在构建期即暴露、可排查；
- 未配置 ``api_key_env`` → ``api_key=None`` 交 litellm 按模型前缀自动解析，
  请求期失败由 litellm 抛错。
"""

from os import environ

from langchain_core.language_models import BaseChatModel
from langchain_litellm import ChatLiteLLM

from app.config.logging.logger import log
from app.core.llm.model_settings import ModelSettings

__all__ = [
    "build_chat_model",
]

# 模型请求超时（秒）：推理模型思考可能耗时较长，取 120s 作为合理默认值；
# ``ModelSettings`` 不承载 timeout 字段，故此处用模块常量，供未来升级为可配置项。
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0


def build_chat_model(
    model_name: str,
    model_settings: ModelSettings | None = None,
) -> BaseChatModel:
    """按模型名称构建 LangChain chat model（ChatLiteLLM 单一收口）。

    屏蔽不同模型的 ``thinking`` / 采样参数差异，返回的对象同时支持流式
    （``.astream()``）与非流式（``.ainvoke()``）调用。Agent 级覆盖配置
    （``model_settings``）若存在，会覆盖模型默认的对应构造项。

    参数:
        model_name: 上层传入的模型名称（带 provider 前缀，如
            ``deepseek/deepseek-v4-flash``），原样透传给 litellm 路由。
        model_settings: 可选 Agent 级模型覆盖配置；其中 ``api_key_env`` 显式配置
            时决定 Key 来源，``base_url`` 作为可选 api_base 覆盖，``thinking`` /
            ``temperature`` / ``top_p`` / ``max_tokens`` 作为采样与思考配置。

    返回:
        一个 LangChain ``BaseChatModel``（``ChatLiteLLM``）实例（每次调用返回新实例，
        可安全复用）。

    异常:
        ValueError: 当 ``model_settings.api_key_env`` 显式配置但对应环境变量缺失
            （或为空）时抛出，消息包含缺失变量名与模型名。

    副作用:
        读取进程环境变量中的 API Key；写一条结构化构建日志（``llm_model_selected``）
        或缺失 Key 的 error 日志（``llm_api_key_missing``）。
    """

    api_key_env = model_settings.api_key_env if model_settings is not None else None
    api_key = environ.get(api_key_env) if api_key_env else None
    if api_key_env and not api_key:
        # api_key_env 显式配置但环境变量缺失/为空：构建期显式报错，不回退 fake 模型，
        # 避免配置错误被静默掩盖（决策 1）。
        log.error(
            "llm_api_key_missing",
            extra={
                "msg": (
                    f"api_key_env 配置但环境变量缺失，拒绝构建模型，model={model_name}，"
                    f"api_key_env={api_key_env}"
                ),
                "data": {
                    "model": model_name,
                    "api_key_env": api_key_env,
                    "env_var_present": environ.get(api_key_env) is not None,
                },
            },
        )
        raise ValueError(
            f"环境变量 {api_key_env} 未配置，无法构建模型 {model_name}；"
            "请设置该环境变量后重试（api_key_env 显式配置但环境变量缺失）。"
        )

    base_url = model_settings.base_url if model_settings is not None else None
    temperature = model_settings.temperature if model_settings is not None else None
    top_p = model_settings.top_p if model_settings is not None else None
    max_tokens = model_settings.max_tokens if model_settings is not None else None
    thinking = model_settings.thinking if model_settings is not None else None

    model_kwargs: dict = {}
    if thinking:
        model_kwargs["thinking"] = {"type": "enabled"}

    log.info(
        "llm_model_selected",
        extra={
            "msg": f"选用 ChatLiteLLM 构建模型，model={model_name}",
            "data": {
                "model": model_name,
                "api_key_env": api_key_env,
                "api_key_provided": api_key is not None,
                "base_url": base_url,
                "thinking": thinking,
            },
        },
    )
    # model_name 原样透传（带 provider 前缀），路由/连接/缓存字段全交 litellm；
    # api_key 为 None 时 litellm 按前缀自动解析环境变量，请求期失败由 litellm 抛错。
    return ChatLiteLLM(
        model=model_name,
        api_key=api_key,
        api_base=base_url,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        model_kwargs=model_kwargs,
        request_timeout=_DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )
