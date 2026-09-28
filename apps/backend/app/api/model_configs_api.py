"""模型连接配置 HTTP API。

本模块只负责请求校验、依赖装配和错误映射；配置持久化与真实网络测试由
``ModelConfigService`` 负责。接口不暴露厂商目录；本机编辑响应会返回 API Key，
但日志和领域摘要始终不包含密钥内容。
"""

from fastapi import Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from app.api.schemas.request.ModelConfigCreateRequest import ModelConfigCreateRequest
from app.api.schemas.request.ModelConfigTestRequest import ModelConfigTestRequest
from app.api.schemas.request.ModelConfigUpdateRequest import ModelConfigUpdateRequest
from app.api.schemas.response.ModelConfigResponse import ModelConfigResponse
from app.app import app
from app.core.llm_provider.capability.model_capability import ModelCapability
from app.service.depends import get_model_config_service
from app.service.model_config import ModelConfigService


def _response(record) -> ModelConfigResponse:
    """将配置记录和可选静态模型能力合并为前端选择器响应。"""

    response = ModelConfigResponse.from_record(record)
    capability = ModelCapability.get_capability(record.model_name)
    response.supports_thinking = capability.supports_thinking
    response.supports_image = capability.supports_image
    response.supports_video = capability.supports_video
    response.supports_reasoning_effort = capability.reasoning_effort.supported
    return response


@app.get("/model-configs", response_model=list[ModelConfigResponse])
async def list_model_configs(
    service: ModelConfigService = Depends(get_model_config_service),
) -> list[ModelConfigResponse]:
    """返回启用和停用的模型连接配置摘要。"""

    return [_response(item) for item in service.list_configs(enabled=True)]


@app.post("/model-configs", response_model=ModelConfigResponse)
async def create_model_config(
    payload: ModelConfigCreateRequest,
    service: ModelConfigService = Depends(get_model_config_service),
) -> ModelConfigResponse:
    """创建模型连接配置。"""

    try:
        record = service.create_config(**payload.model_dump())
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="模型配置名称已存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _response(record)


@app.put("/model-configs/{config_id}", response_model=ModelConfigResponse)
async def update_model_config(
    config_id: int,
    payload: ModelConfigUpdateRequest,
    service: ModelConfigService = Depends(get_model_config_service),
) -> ModelConfigResponse:
    """更新模型连接配置的显式字段。"""

    try:
        record = service.update_config(
            config_id,
            **{key: value for key, value in payload.model_dump().items() if value is not None},
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="模型配置不存在") from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="模型配置名称已存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _response(record)


@app.delete("/model-configs/{config_id}")
async def delete_model_config(
    config_id: int,
    service: ModelConfigService = Depends(get_model_config_service),
) -> dict[str, object]:
    """幂等删除模型连接配置。"""

    service.delete_config(config_id)
    return {"config_id": config_id, "deleted": True}


@app.post("/model-configs/test")
async def test_draft_model_config(
    payload: ModelConfigTestRequest,
    service: ModelConfigService = Depends(get_model_config_service),
) -> dict[str, object | None]:
    """对尚未保存的配置执行一次真实 OpenAI 兼容请求。"""

    result = await service.test_draft(**payload.model_dump())
    return {
        "config_id": result.config_id,
        "success": result.success,
        "elapsed_ms": result.elapsed_ms,
        "error_code": result.error_code,
        "error_message": result.error_message,
    }


@app.post("/model-configs/{config_id}/test")
async def test_model_config(
    config_id: int,
    service: ModelConfigService = Depends(get_model_config_service),
) -> dict[str, object | None]:
    """对已保存的配置执行一次真实 OpenAI 兼容请求。"""

    try:
        record = service.get_config(config_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="模型配置不存在") from exc
    result = await service.test_connection(record)
    return {
        "config_id": result.config_id,
        "success": result.success,
        "elapsed_ms": result.elapsed_ms,
        "error_code": result.error_code,
        "error_message": result.error_message,
    }
