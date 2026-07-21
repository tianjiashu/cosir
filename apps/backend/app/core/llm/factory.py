"""模型工厂：构建 LangChain chat model。"""

import os

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from app.config.settings import BackendSettings


def build_chat_model(settings: BackendSettings) -> BaseChatModel:
    """根据后端配置构建 LangChain chat model。

    echo 模式或缺少 API Key 时返回 ``GenericFakeChatModel``，保证默认配置下可本地
    启动与单测可跑；DeepSeek / OpenAI 兼容端点返回 ``ChatOpenAI`` 直连，thinking
    私有参数按 ``base_url`` 是否含 ``deepseek`` 条件注入。

    参数:
        settings: 后端模型相关配置。

    返回:
        一个 LangChain ``BaseChatModel`` 实例（每次调用返回新实例，可安全复用）。

    异常:
        ValueError: 如果服务商不受支持。

    副作用:
        读取进程环境变量中的 API Key。
    """

    if settings.model_provider == "echo" or not os.environ.get(settings.model_api_key_env):
        return GenericFakeChatModel(
            messages=iter([AIMessage(content="收到任务，已记录并开始处理。")])
        )

    if settings.model_provider == "openai-compatible":
        extra: dict = {}
        if "deepseek" in settings.model_base_url:
            extra["thinking"] = {"type": settings.model_thinking_mode}
        return ChatOpenAI(
            model=settings.model_name,
            base_url=settings.model_base_url,
            api_key=os.environ.get(settings.model_api_key_env, ""),
            streaming=True,
            **extra,
        )

    raise ValueError(f"unsupported model provider: {settings.model_provider}")
