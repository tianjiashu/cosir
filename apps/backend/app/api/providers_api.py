"""模型厂商（Provider）域 HTTP 端点。

承载厂商配置中心的后端接口（设计文档 §8.1）：厂商 CRUD + litellm 目录发现。
模型条目（models 表）的导入与单条管理虽以 ``/providers/{id}/models`` 为部分
路径前缀，但按「实体内聚」原则收口在 ``models_api``（模型条目实体归属）。

错误语义（设计文档 §8.4）：
- Provider 保存**不校验** Key 非空（允许先配置后填 Key，D9）；``GET /
  /providers`` 返回 ``api_key_configured`` 供前端状态展示。Key 明文只进
  ``providers.api_key`` 列，响应与日志永不回传明文。
- discover 是外部依赖调用（litellm 目录），失败转 502 级响应 + 引导重试，
  不静默返回空列表（§7.1）。
"""

from fastapi import Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import (
    get_model_entry_service,
    get_provider_connection_test_service,
    get_provider_discover_service,
    get_provider_service,
)
from app.api.schemas.request.ProviderCreateRequest import ProviderCreateRequest
from app.api.schemas.request.ProviderUpdateRequest import ProviderUpdateRequest
from app.api.schemas.response.ModelCandidateResponse import ModelCandidateResponse
from app.api.schemas.response.ProviderResponse import ProviderResponse
from app.app import app
from app.config.logging.logger import log
from app.llm_provider.provider import ModelEntryService
from app.llm_provider.provider.provider_connection_test_service import ProviderConnectionTestService, \
    ConnectionTestResult
from app.llm_provider.provider import ProviderDiscoverService, ProviderDiscoverError
from app.llm_provider.provider import ProviderService


@app.get("/providers")
async def list_providers(
    provider_service: ProviderService = Depends(get_provider_service),
    model_entry_service: ModelEntryService = Depends(get_model_entry_service),
) -> list[ProviderResponse]:
    """返回厂商列表（聚合模型数量与 ``api_key_configured`` 状态）。

    参数:
        provider_service: 通过依赖注入的厂商 service。
        model_entry_service: 通过依赖注入的模型条目 service（聚合模型数量）。

    返回:
        ``ProviderResponse`` 列表（含禁用厂商；排序按 ``sort_order`` 升序）。

    异常:
        无。

    副作用:
        无。
    """

    providers = provider_service.list_providers()
    all_models = model_entry_service.list_models()
    model_count_by_provider: dict[str, int] = {}
    for model in all_models:
        model_count_by_provider[model.provider_id] = (
            model_count_by_provider.get(model.provider_id, 0) + 1
        )
    return [
        ProviderResponse.from_record(
            record,
            api_key_configured=provider_service.api_key_configured(record),
            model_count=model_count_by_provider.get(record.provider_id, 0),
        )
        for record in providers
    ]


@app.post("/providers")
async def create_provider(
    payload: ProviderCreateRequest,
    provider_service: ProviderService = Depends(get_provider_service),
) -> ProviderResponse:
    """新建模型厂商。

    参数:
        payload: 包含 name / type / base_url / api_key 的请求体。
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
            provider_type=payload.type,
            base_url=payload.base_url,
            api_key=payload.api_key,
            enabled=payload.enabled,
            sort_order=payload.sort_order,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
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
    provider_id: str,
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
            name=payload.name,
            provider_type=payload.type,
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
    provider_id: str,
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


@app.post("/providers/{provider_id}/discover")
async def discover_provider_models(
    provider_id: str,
    provider_service: ProviderService = Depends(get_provider_service),
    discover_service: ProviderDiscoverService = Depends(get_provider_discover_service),
) -> list[ModelCandidateResponse]:
    """读取 litellm 模型目录并按厂商类型过滤出候选模型（D3）。

    目录 =「litellm 支持的模型」≠「厂商实际提供」（§14）；候选仅供勾选导入，
    最终以请求期结果为真。目录读取失败转 502（不静默返回空列表，§7.1）。

    参数:
        provider_id: 来自路由的厂商标识。
        provider_service: 通过依赖注入的厂商 service（存在性守卫）。
        discover_service: 通过依赖注入的目录发现 service。

    返回:
        ``ModelCandidateResponse`` 列表（含 ``already_imported`` 标注；已导入
        项排序列表尾部）。

    异常:
        HTTPException: 厂商不存在（404）或 litellm 目录读取失败（502）时抛出。

    副作用:
        首次调用会惰性加载 litellm 目录（进程内后续复用）；成功 / 失败日志由
        service 层统一记录。
    """

    try:
        provider = provider_service.get_provider(provider_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="provider not found") from exc
    try:
        candidates = discover_service.discover_models(provider)
    except ProviderDiscoverError as exc:
        log.error(
            "provider_discover_api_failed",
            extra={
                "msg": "discover 端点返回 502：litellm 目录读取失败",
                "data": {
                    "provider_id": provider_id,
                    "type": provider.provider_type,
                },
            },
        )
        raise HTTPException(
            status_code=502,
            detail="litellm model catalog unavailable; please retry later",
        ) from exc
    return [ModelCandidateResponse.from_candidate(candidate) for candidate in candidates]


@app.post("/providers/{provider_id}/test")
async def test_provider_connection(
    provider_id: str,
    provider_service: ProviderService = Depends(get_provider_service),
    connection_test_service: ProviderConnectionTestService = Depends(
        get_provider_connection_test_service
    ),
) -> dict[str, object]:
    """对厂商发起一次最小 chat 请求验证凭据 / 端点可用性（设计文档 §三 用户视角三件套）。

    用已配置参数（``base_url`` / ``api_key``）发起一次
    ``max_tokens=1`` 的 ping 请求，返回 ``success`` / ``error_code`` /
    ``error_message`` / ``elapsed_ms``。**失败是正常结果之一**（区别于
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
    try:
        result: ConnectionTestResult = await connection_test_service.test_connection(provider)
    except NotImplementedError as exc:
        log.warning(
            "provider_connection_test_not_implemented",
            extra={
                "msg": "连通性测试端点骨架阶段未实现，返回 501",
                "data": {
                    "provider_id": provider_id,
                    "type": provider.provider_type,
                },
            },
        )
        raise HTTPException(
            status_code=501,
            detail="provider connection test not implemented yet (skeleton phase)",
        ) from exc
    return {
        "provider_id": result.provider_id,
        "success": result.success,
        "elapsed_ms": result.elapsed_ms,
        "error_code": result.error_code,
        "error_message": result.error_message,
    }
