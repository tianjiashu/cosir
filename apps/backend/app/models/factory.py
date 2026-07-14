"""模型适配器工厂函数。"""

from app.config.settings import BackendSettings
from app.models.echo import EchoStreamingModelAdapter
from app.models.openai_compatible import (
    OpenAICompatibleModelConfig,
    OpenAICompatibleStreamingAdapter,
)
from app.models.base import StreamingModelAdapter


def build_model_adapter(settings: BackendSettings) -> StreamingModelAdapter:
    """构建配置好的流式模型适配器。

    参数:
        settings: 包含模型服务商选择的后端配置。

    返回:
        由 ``settings.model_provider`` 选定的流式模型适配器。

    异常:
        ValueError: 如果配置的服务商不受支持。

    副作用:
        无。
    """

    if settings.model_provider == "echo":
        return EchoStreamingModelAdapter()
    if settings.model_provider == "openai-compatible":
        return build_openai_compatible_adapter(settings)
    raise ValueError(f"unsupported model provider: {settings.model_provider}")


def build_openai_compatible_adapter(
    settings: BackendSettings,
) -> OpenAICompatibleStreamingAdapter:
    """基于配置构建 OpenAI 兼容的流式模型适配器。

    参数:
        settings: 包含模型端点、API Key 环境变量和模型名的后端配置。

    返回:
        为所选服务商配置好的 OpenAICompatibleStreamingAdapter。

    异常:
        无。缺失的 API Key 会在此适配器被使用时报告。

    副作用:
        无。
    """

    return OpenAICompatibleStreamingAdapter(
        OpenAICompatibleModelConfig(
            base_url=settings.model_base_url,
            api_key_env=settings.model_api_key_env,
            model=settings.model_name,
            thinking_mode=settings.model_thinking_mode,
        )
    )
