"""模型厂商（Provider）域 HTTP 端点。

承载厂商配置中心的后端接口（设计文档 §8.1）：厂商 CRUD + litellm 目录发现。
模型条目（models 表）的导入与单条管理虽以 ``/providers/{id}/models`` 为部分
路径前缀，但按「实体内聚」原则收口在 ``models_api``（模型条目实体归属）。
"""

from fastapi import Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import get_provider_service
from app.api.schemas.request.ProviderCreateRequest import ProviderCreateRequest
from app.api.schemas.request.ProviderUpdateRequest import ProviderUpdateRequest
from app.api.schemas.response.ProviderCapabilityResponse import ProviderCapabilityResponse
from app.api.schemas.response.ProviderResponse import ProviderResponse
from app.app import app
from app.core.llm_provider.capability.provider_capability import (
    _SUPPORT_PROVIDERS,
    ProviderCapability,
)
from app.service.provider import ProviderService
from app.service.provider import ConnectionTestResult


@app.get("/providers/catalog", response_model=list[ProviderCapabilityResponse])
async def list_provider_catalog() -> list[ProviderCapabilityResponse]:
    """返回由 ``llm_provider.json`` 注册的可配置 Provider 目录。"""

    return [
        ProviderCapabilityResponse(
            name=name,
            provider_type=capability.provider_type,
            default_base_url=capability.default_base_url,
            requires_api_key=capability.requires_api_key,
            models=list(capability.models),
        )
        for name in sorted(_SUPPORT_PROVIDERS)
        for capability in [ProviderCapability.get_capability(name)]
    ]


@app.get("/providers", response_model=list[ProviderResponse])
async def list_providers(
    provider_service: ProviderService = Depends(get_provider_service),
) -> list[ProviderResponse]:
    """返回 Provider 配置列表（模型中心管理面板的数据源）。

    参数:
        provider_service: 通过依赖注入的厂商 service。

    返回:
        Provider 配置列表。响应只包含 API Key 是否已配置，不返回 Key 明文。

    异常:
        无。

    副作用:
        只读 Provider 配置。
    """

    providers = provider_service.list_providers()
    return [
        ProviderResponse.from_record(
            provider,
            api_key_configured=provider_service.api_key_configured(provider),
        )
        for provider in providers
    ]


@app.post("/providers")
async def create_provider(
    payload: ProviderCreateRequest,
    provider_service: ProviderService = Depends(get_provider_service),
) -> ProviderResponse:
    """新建模型厂商。

    参数:
        payload: 包含 name / base_url / api_key 的请求体。
        provider_service: 通过依赖注入的厂商 service。

    返回:
        创建后的 ``ProviderResponse``。

    异常:
        HTTPException: 输入非法（400）或厂商重名（409）时抛出。

    副作用:
        在存储中创建厂商（service 层写 ``provider_created`` 审计日志）。
    """

    try:
        record = provider_service.create_provider(
            name=payload.name,
            base_url=payload.base_url,
            api_key=payload.api_key,
            sort_order=payload.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        if "providers.name" not in str(exc.orig):
            raise HTTPException(status_code=500, detail="provider persistence failed") from exc
        raise HTTPException(
            status_code=409,
            detail=f"provider name conflict: {payload.name}",
        ) from exc
    return ProviderResponse.from_record(
        record,
        api_key_configured=provider_service.api_key_configured(record),
    )


@app.put("/providers/{provider_id}")
async def update_provider(
    provider_id: int,
    payload: ProviderUpdateRequest,
    provider_service: ProviderService = Depends(get_provider_service),
) -> ProviderResponse:
    """更新厂商字段（仅覆盖显式传入字段；启停即时生效）。

    参数:
        provider_id: 来自路由的厂商标识。
        payload: 待更新字段的请求体（None = 不更新，``""`` = 清除）。
        provider_service: 通过依赖注入的厂商 service。

    返回:
        更新后的 ``ProviderResponse``。

    异常:
        HTTPException: 厂商不存在（404）、输入非法（400）或重名冲突（409）时抛出。

    副作用:
        更新 ``providers`` 表对应行（service 层写 ``provider_updated`` 审计日志）。
    """

    try:
        record = provider_service.update_provider(
            provider_id,
            base_url=payload.base_url,
            api_key=payload.api_key,
            enabled=payload.enabled,
            sort_order=payload.sort_order,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="provider not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="provider name conflict") from exc
    return ProviderResponse.from_record(
        record,
        api_key_configured=provider_service.api_key_configured(record),
    )


@app.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: int,
    provider_service: ProviderService = Depends(get_provider_service),
) -> dict[str, object]:
    """删除厂商（其下模型条目由 FK CASCADE 级联删除）。

    参数:
        provider_id: 来自路由的厂商标识。
        provider_service: 通过依赖注入的厂商 service。

    返回:
        ``{"provider_id": ..., "deleted": true}``（幂等：厂商不存在也返回成功）。

    异常:
        无。

    副作用:
        级联删除厂商与其下模型条目（service 层写 ``provider_deleted`` 审计日志）。
    """

    provider_service.delete_provider(provider_id)
    return {"provider_id": provider_id, "deleted": True}


@app.post("/providers/{provider_id}/test")
async def test_provider_connection(
    provider_id: int, provider_service: ProviderService = Depends(get_provider_service)
) -> dict[str, object]:
    """对厂商发起一次最小 chat 请求验证凭据 / 端点可用性（设计文档 §三 用户视角三件套）。

    用已配置参数（``base_url`` / ``api_key``）发起一次
    ``max_tokens=1`` 的 ping 请求，返回 ``success`` ``elapsed_ms``。**失败是正常结果之一**（区别于
    discover 的 502：测试本身就是「试错」语义），故失败也返回 200 携带
    ``success=False``，由前端据 ``error_code`` 给出修复引导。

    参数:
        provider_id: 来自路由的厂商标识。
        provider_service: 通过依赖注入的厂商 service（存在性守卫）。
        connection_test_service: 通过依赖注入的连通性测试 service。

    返回:
        ``ConnectionTestResult`` 字段平铺的字典（``provider_id`` / ``success`` /
        ``elapsed_ms`` / ``error_code`` / ``error_message``）。骨架阶段恒抛
        ``NotImplementedError`` 由 FastAPI 转 501（阶段 2 实施后改为正常返回）。

    异常:
        HTTPException: 厂商不存在（404）或骨架阶段未实现（501）时抛出。

    副作用:
        发起一次到厂商端点的网络请求（阶段 2 实施后）；写 info 级
        ``provider_connection_test_*`` 日志（由 service 层记录）。
    """

    try:
        provider = provider_service.get_provider(provider_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="provider not found") from exc
    result: ConnectionTestResult = await provider_service.test_connection(provider)
    return {
        "provider_id": result.provider_id,
        "success": result.success,
        "elapsed_ms": result.elapsed_ms,
        "error_code": result.error_code,
        "error_message": result.error_message,
    }
