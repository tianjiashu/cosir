"""模型工厂：按模型名称构建 LangChain chat model（屏蔽不同模型/服务商的差异）。

上层只需传入模型名称（如 ``deepseek-v4-flash`` / ``deepseek-v4-pro``），工厂从模型注册表
``supported_models`` 解析规格，并委托对应 ``LLMProvider`` 构建 LangChain ``BaseChatModel``。
返回的模型对象天然同时支持流式（``.astream()``）与非流式（``.ainvoke()``）调用，由调用方
决定输入输出的流/非流形态——工厂本身只负责「按名取模型」，不掺入编排、工具绑定或事件翻译
（那些留在 ``workflow`` / ``langchain_bridge``）。

缺 API Key 时回退到 ``GenericFakeChatModel``，保证默认本地无 Key 启动与单测可跑。
"""

import os

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.core.llm.llm_provider.deepseek_provider import DeepSeekProvider
from app.core.llm.model_settings import ModelSettings

__all__ = [
    "build_chat_model",
]


def build_chat_model(
    model_name: str,
    model_settings: ModelSettings | None = None,
) -> BaseChatModel:
    """按模型名称构建 LangChain chat model。

    屏蔽不同模型的 ``base_url`` / ``thinking`` 等差异，返回的对象同时支持流式
    （``.astream()``）与非流式（``.ainvoke()``）调用。缺 API Key 时回退到
    ``GenericFakeChatModel``，保证本地无 Key 启动与单测通过。Agent 级覆盖配置
    （``model_settings``）若存在，会覆盖注册表的对应规格项。

    参数:
        model_name: 上层传入的模型名称（如 ``deepseek-v4-flash``）。
        settings: 后端配置（提供全局默认值）。
        model_settings: 可选 Agent 级模型覆盖配置。

    返回:
        一个 LangChain ``BaseChatModel`` 实例（每次调用返回新实例，可安全复用）。

    异常:
        ValueError: 如果模型名称不受支持。

    副作用:
        读取进程环境变量中的 API Key。
    """

    api_key = model_settings.api_key_env
    if not api_key:
        return GenericFakeChatModel(
            messages=iter([AIMessage(content="收到任务，已记录并开始处理。")])
        )
    return DeepSeekProvider().build(model_name, model_settings)
