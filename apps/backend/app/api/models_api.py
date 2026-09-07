"""模型目录 HTTP 端点。

模型目录不读取 ``models`` 表。Provider 配置决定哪些厂商参与目录，
``ProviderCapability`` 决定厂商支持的模型集合，``ModelCapability`` 决定模型能力。
"""

from fastapi import Depends

from app.api.schemas.response.ModelListItemResponse import ModelListItemResponse
from app.api.schemas.response.ProviderModelGroupResponse import (
    ProviderModelGroupResponse,
)
from app.app import app
from app.config.logging.logger import log
from app.core.llm_provider.capability.model_capability import ModelCapability
from app.core.llm_provider.capability.provider_capability import ProviderCapability
from app.service.depends import get_provider_service
from app.service.provider import ProviderService


@app.get("/models")
async def list_models(
    provider_service: ProviderService = Depends(get_provider_service),
) -> list[ProviderModelGroupResponse]:
    """返回按启用模型厂商聚合的模型目录。

    参数:
        provider_service: 通过依赖注入的厂商 service。

    返回:
        Provider 分组列表。每个模型只返回模型名和前端选择所需的静态能力标记。

    异常:
        无。未注册 Provider 能力的 Provider 不贡献模型分组，并记录可排查日志。

    副作用:
        只读 Provider 配置和进程内能力注册表。
    """

    providers = provider_service.list_providers(enabled=True)
    responses: list[ProviderModelGroupResponse] = []

    for provider in providers:
        if provider.id is None:
            log.warning(
                "models_provider_id_missing",
                extra={
                    "msg": "模型目录跳过缺少 id 的 Provider",
                    "data": {"provider_name": provider.name},
                },
            )
            continue
        try:
            provider_capability = ProviderCapability.get_capability(provider.name)
        except ValueError:
            log.warning(
                "models_provider_capability_missing",
                extra={
                    "msg": "模型目录跳过未注册能力的 Provider",
                    "data": {
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                    },
                },
            )
            continue

        models: list[ModelListItemResponse] = []
        for model_name in provider_capability.models:
            model_capability = ModelCapability.get_capability(model_name)
            supports_reasoning_effort = model_capability.reasoning_effort.supported and {
                "low",
                "high",
                "max",
            }.issubset(model_capability.reasoning_effort.effort_map)
            models.append(
                ModelListItemResponse(
                    model_name=model_name,
                    supports_thinking=model_capability.supports_thinking,
                    supports_image=model_capability.supports_image,
                    supports_video=model_capability.supports_video,
                    supports_reasoning_effort=supports_reasoning_effort,
                )
            )
        if models:
            responses.append(
                ProviderModelGroupResponse(
                    provider_id=provider.id,
                    provider_name=provider.name,
                    models=models,
                )
            )

    return responses
