"""模型条目（Model Entry）域 HTTP 端点。

承载模型下拉数据源与模型条目管理接口（设计文档 §8.2）：启用模型扁平列表、
按厂商批量导入（discover 勾选 / 手动添加，路径挂在 ``/providers/{id}/models``
下但按「实体内聚」归本模块）、单条更新与删除。

错误语义（设计文档 §8.4）：
- ``GET /models`` 返回空列表 ≠ 错误：前端据此驱动空态引导（去配置中心添加）。
- 下拉只显示「模型启用 **且** 归属厂商启用」的条目（与解析链可命中集合一致，
  避免下拉可选但发送被 422 拒绝的歧义）。
"""

from fastapi import Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import (
    get_model_entry_service,
    get_provider_service,
)
from app.api.schemas.request.ModelBulkImportRequest import ModelBulkImportRequest
from app.api.schemas.request.ModelUpdateRequest import ModelUpdateRequest
from app.api.schemas.response.ModelEntryResponse import ModelEntryResponse
from app.api.schemas.response.ModelImportResponse import ModelImportResponse
from app.app import app
from app.llm_provider.provider import ModelEntryService, ProviderService
from app.models.model_entry_record import ModelEntryRecord
from app.models.provider_record import ProviderRecord


@app.get("/models")
async def list_models(
        model_entry_service: ModelEntryService = Depends(get_model_entry_service),
        provider_service: ProviderService = Depends(get_provider_service),
) -> list[ModelEntryResponse]:
    """返回启用模型扁平列表（模型选择下拉数据源）。

    过滤口径：模型 ``enabled`` 且归属厂商 ``enabled``（与解析链可命中集合
    一致）；条目按厂商分组展示由前端按 ``provider_name`` 聚合。返回空列表
    不是错误（前端驱动「未配置任何模型」空态引导，§8.4）。

    参数:
        model_entry_service: 通过依赖注入的模型条目 service。
        provider_service: 通过依赖注入的厂商 service（厂商过滤与 Key 状态）。

    返回:
        ``ModelEntryResponse`` 列表（含厂商名与 ``api_key_configured``，供
        发送前 Key 校验，§9）。

    异常:
        无。

    副作用:
        无。
    """

    providers = {
        provider.id: provider for provider in provider_service.list_providers(enabled=True)
    }
    models = model_entry_service.list_models(enabled=True)

    return [
        ModelEntryResponse.from_records(
            model,
            providers[model.provider_id],
            api_key_configured=provider_service.api_key_configured(
                providers[model.provider_id]
            ),
        )
        for model in models
    ]


@app.post("/providers/{provider_id}/models")
async def import_provider_models(
        provider_id: str,
        payload: ModelBulkImportRequest,
        model_entry_service: ModelEntryService = Depends(get_model_entry_service),
        provider_service: ProviderService = Depends(get_provider_service),
) -> ModelImportResponse:
    """按厂商批量导入模型条目（discover 勾选或手动添加统一入口，D6）。

    同厂商下已存在的同名模型被跳过（幂等导入），不会整批报错；其余条目单
    事务写入，任一违约整批回滚。

    参数:
        provider_id: 来自路由的导入目标厂商标识。
        payload: 待导入条目列表请求体。
        model_entry_service: 通过依赖注入的模型条目 service。
        provider_service: 通过依赖注入的厂商 service（存在性守卫 + Key 状态）。

    返回:
        ``ModelImportResponse``（成功导入条目 + 跳过的重名模型）。

    异常:
        HTTPException: 厂商不存在（404）、字段非法（400）或批内重名 / FK
            违约（409）时抛出。

    副作用:
        向 ``models`` 表批量插入行（service 层写 ``models_imported`` 审计日志）。
    """

    try:
        provider = provider_service.get_provider(provider_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="provider not found") from exc
    try:
        result = model_entry_service.import_models(
            provider_id,
            [model.model_dump() for model in payload.models],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="model import conflict: duplicate model_name or unknown provider",
        ) from exc
    return ModelImportResponse.from_result(
        result,
        provider,
        api_key_configured=provider_service.api_key_configured(provider),
    )


@app.put("/models/{model_id}")
async def update_model(
        model_id: str,
        payload: ModelUpdateRequest,
        model_entry_service: ModelEntryService = Depends(get_model_entry_service),
        provider_service: ProviderService = Depends(get_provider_service),
) -> ModelEntryResponse:
    """更新模型条目字段（仅覆盖显式传入字段）。

    参数:
        model_id: 来自路由的模型条目标识。
        payload: 待更新字段的请求体。
        model_entry_service: 通过依赖注入的模型条目 service。
        provider_service: 通过依赖注入的厂商 service（响应聚合厂商信息）。

    返回:
        更新后的 ``ModelEntryResponse``。

    异常:
        HTTPException: 条目不存在（404）或字段非法（400）时抛出。

    副作用:
        更新 ``models`` 表对应行（service 层写 ``model_updated`` 审计日志）。
    """

    try:
        record = model_entry_service.update_model(
            model_id,
            display_name=payload.display_name,
            max_context_window=payload.max_context_window,
            supports_thinking=payload.supports_thinking,
            enabled=payload.enabled,
            sort_order=payload.sort_order,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="model not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(record, provider_service)


@app.delete("/models/{model_id}")
async def delete_model(
        model_id: str,
        model_entry_service: ModelEntryService = Depends(get_model_entry_service),
) -> dict[str, object]:
    """删除单个模型条目。

    参数:
        model_id: 来自路由的模型条目标识。
        model_entry_service: 通过依赖注入的模型条目 service。

    返回:
        ``{"model_id": ..., "deleted": true}``（幂等：条目不存在也返回成功）。

    异常:
        无。

    副作用:
        从 ``models`` 表删除对应行（service 层写 ``model_deleted`` 审计日志）。
    """

    model_entry_service.delete_model(model_id)
    return {"model_id": model_id, "deleted": True}


def _to_response(
        record: ModelEntryRecord,
        provider_service: ProviderService,
) -> ModelEntryResponse:
    """把模型条目记录转换为响应模型（补齐归属厂商聚合信息）。

    参数:
        record: 待转换的模型条目记录。
        provider_service: 厂商 service（查归属厂商与 Key 状态）。

    返回:
        与记录字段对齐并带厂商聚合信息的 ``ModelEntryResponse``；归属厂商
        被并发删除的极端场景下以 ``provider_id`` 兜底展示，不阻断响应。

    异常:
        无。

    副作用:
        无。
    """

    try:
        provider: ProviderRecord | None = provider_service.get_provider(record.provider_id)
    except KeyError:
        provider = None
    if provider is None:
        return ModelEntryResponse(
            model_id=record.id,
            provider_id=record.provider_id,
            provider_name=str(record.provider_id),
            model_name=record.model_name,
            display_name=record.display_name,
            max_context_window=record.max_context_window,
            supports_thinking=record.supports_thinking,
            enabled=record.enabled,
            api_key_configured=True,
            sort_order=record.sort_order,
        )
    return ModelEntryResponse.from_records(
        record,
        provider,
        api_key_configured=provider_service.api_key_configured(provider),
    )
