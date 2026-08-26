"""模型条目（Model Entry）域 HTTP 端点。
"""
import dataclasses

from fastapi import Depends

from app.api.dependencies import (
    get_model_entry_service,
    get_provider_service,
)
from app.api.schemas.response.ModelEntryResponse import ModelEntryResponse
from app.app import app
from app.llm_provider.capability.model_capability import ModelCapability
from app.llm_provider.capability.provider_capability import ProviderCapability
from app.llm_provider.provider import ModelEntryService, ProviderService


@app.get("/models")
async def list_models(
        model_entry_service: ModelEntryService = Depends(get_model_entry_service),
        provider_service: ProviderService = Depends(get_provider_service),
) -> list[ModelEntryResponse]:
    """返回启用模型扁平列表（模型选择下拉数据源）。

    参数:
        model_entry_service: 通过依赖注入的模型条目 service。
        provider_service: 通过依赖注入的厂商 service（厂商过滤与 Key 状态）。

    返回:
        ``ModelEntryResponse`` 列表

    异常:
        无。

    副作用:
        无。
    """

    providers = provider_service.list_providers(enabled=True)

    responses = []

    for provider in providers:
        provider_capability = ProviderCapability.get_capability(provider.name)
        model_names = provider_capability.models
        for model_name in model_names:
            model_capability = ModelCapability.get_capability(model_name)
            responses.append(ModelEntryResponse(
                provider_id=provider.id,
                provider_name=provider.name,
                model_name=model_name,
                supports_thinking=model_capability.supports_thinking,
                supports_image=model_capability.supports_image,
                supports_video=model_capability.supports_video,
                enabled=True,
                api_key_configured=provider_service.api_key_configured(provider),
                reasoning_effort=dataclasses.asdict(model_capability.reasoning_effort),
                sort_order=0,
            ))

    return responses
