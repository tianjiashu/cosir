"""模型工厂：按模型名称构建 LangChain chat model（屏蔽不同模型/服务商的差异）。

上层只需传入模型名称（如 ``deepseek-v4-flash`` / ``deepseek-v4-pro``），工厂从受支持模型表
``supported_models`` 解析出 ``base_url`` / ``api_key`` / ``thinking`` 等差异并构建 LangChain
``BaseChatModel``。返回的模型对象天然同时支持流式（``.astream()``）与非流式（``.ainvoke()``）
调用，由调用方决定输入输出的流/非流形态——工厂本身只负责「按名取模型」，不掺入编排、工具
绑定或事件翻译（那些留在 ``workflow`` / ``langchain_bridge``）。

缺 API Key 时回退到 ``GenericFakeChatModel``，保证默认本地无 Key 启动与单测可跑。
"""

import os
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from app.config.settings import BackendSettings


@dataclass(frozen=True)
class ModelSpec:
    """单个模型的能力规格，作为模型工厂的单一事实来源。

    参数:
        name: 对外暴露、由上层传入的模型名称。
        api_name: 实际发送给服务商的模型标识（可与 ``name`` 不同）。
        base_url: OpenAI 兼容服务商的基础 URL。
        api_key_env: 持有服务商 API Key 的环境变量名。
        thinking: DeepSeek thinking 模式，``enabled`` 或 ``disabled``；仅服务商支持时注入。
    """

    name: str
    api_name: str
    base_url: str
    api_key_env: str
    thinking: str = "disabled"


def supported_models(settings: BackendSettings) -> dict[str, ModelSpec]:
    """返回当前受支持的模型规格表（模型名称 → 规格）。

    目前仅 DeepSeek OpenAI 兼容端点，覆盖 ``deepseek-v4-flash`` 与 ``deepseek-v4-pro``
    两档（thinking 差异由本表表达）。后续接入其它服务商时在此表增量添加即可，调用方无需改动。

    参数:
        settings: 后端配置，提供 ``base_url`` 与 ``api_key_env`` 默认值。

    返回:
        以模型名称为键的规格表。
    """

    key_env = settings.model_api_key_env
    base = settings.model_base_url
    return {
        "deepseek-v4-flash": ModelSpec(
            name="deepseek-v4-flash",
            api_name="deepseek-v4-flash",
            base_url=base,
            api_key_env=key_env,
            thinking="disabled",
        ),
        "deepseek-v4-pro": ModelSpec(
            name="deepseek-v4-pro",
            api_name="deepseek-v4-pro",
            base_url=base,
            api_key_env=key_env,
            thinking="enabled",
        ),
    }


def resolve_model_spec(model_name: str, settings: BackendSettings) -> ModelSpec:
    """按模型名称解析规格。

    参数:
        model_name: 上层传入的模型名称。
        settings: 后端配置。

    返回:
        对应的 ``ModelSpec``。

    异常:
        ValueError: 如果模型名称不在受支持列表中。
    """

    specs = supported_models(settings)
    spec = specs.get(model_name)
    if spec is None:
        supported = ", ".join(sorted(specs))
        raise ValueError(f"unsupported model: {model_name}; supported: {supported}")
    return spec


def build_chat_model(model_name: str, settings: BackendSettings) -> BaseChatModel:
    """按模型名称构建 LangChain chat model。

    屏蔽不同模型的 ``base_url`` / ``thinking`` 等差异，返回的对象同时支持流式
    （``.astream()``）与非流式（``.ainvoke()``）调用。缺 API Key 时回退到
    ``GenericFakeChatModel``，保证本地无 Key 启动与单测通过。

    参数:
        model_name: 上层传入的模型名称（如 ``deepseek-v4-flash``）。
        settings: 后端配置。

    返回:
        一个 LangChain ``BaseChatModel`` 实例（每次调用返回新实例，可安全复用）。

    异常:
        ValueError: 如果模型名称不受支持。

    副作用:
        读取进程环境变量中的 API Key。
    """

    spec = resolve_model_spec(model_name, settings)
    api_key = os.environ.get(spec.api_key_env, "")
    if not api_key:
        return GenericFakeChatModel(
            messages=iter([AIMessage(content="收到任务，已记录并开始处理。")])
        )
    extra: dict = {}
    if spec.thinking == "enabled":
        extra["thinking"] = {"type": "enabled"}
    return ChatOpenAI(
        model=spec.api_name,
        base_url=spec.base_url,
        api_key=api_key,
        streaming=True,
        **extra,
    )
