"""配置中心的「模型」子域：连接与其下模型，事实源是系统级模型配置文件。

本模块只负责请求校验、依赖装配和错误映射（统一经 ``raise_configuration_error``：不存在 404、
重名 409、请求或文件无效 400、文件读写失败 500）；文件读写与连接测试由 ``ModelConfigService``
负责，远端模型目录发现由 ``ModelDiscoveryService`` 负责。

模型名可能带 ``/``（如 ``deepseek-ai/DeepSeek-V3``），所以按模型寻址的路由用 ``{model_name:path}``；
POST ``/discover`` 与 ``/test`` 只出现在集合根下，不会和 ``/{connection_id}/models`` 冲突。
"""

from fastapi import Depends

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.request.ModelConfigsAddRequest import ModelConfigsAddRequest
from app.api.schemas.request.ModelConfigTestRequest import ModelConfigTestRequest
from app.api.schemas.request.ModelConfigUpdateRequest import ModelConfigUpdateRequest
from app.api.schemas.request.ModelConnectionCreateRequest import ModelConnectionCreateRequest
from app.api.schemas.request.ModelConnectionUpdateRequest import ModelConnectionUpdateRequest
from app.api.schemas.request.ModelDiscoveryRequest import ModelDiscoveryRequest
from app.api.schemas.response.ModelConfigResponse import ModelConfigResponse
from app.api.schemas.response.ModelConnectionResponse import (
    ModelConfigFileResponse,
    ModelConnectionResponse,
)
from app.api.schemas.response.ModelDiscoveryResponse import ModelDiscoveryResponse
from app.app import app
from app.service.depends import get_model_config_service, get_model_discovery_service
from app.service.model_config import ModelConfigService
from app.service.model_config.model_discovery_service import ModelDiscoveryService

_ROOT = "/configuration/model-connections"


@app.get(_ROOT, response_model=ModelConfigFileResponse)
async def read_model_config_file(
    service: ModelConfigService = Depends(get_model_config_service),
) -> ModelConfigFileResponse:
    """返回文件路径与全部连接（含还没有模型的连接）；文件写坏时 400 并指出出错位置。"""

    try:
        return ModelConfigFileResponse.from_file(str(service.path), service.read())
    except Exception as exc:
        raise_configuration_error(exc)


@app.post(_ROOT, response_model=ModelConnectionResponse)
async def create_model_connection(
    payload: ModelConnectionCreateRequest,
    service: ModelConfigService = Depends(get_model_config_service),
) -> ModelConnectionResponse:
    """新建连接与它的首批模型，返回写入后的连接（含各模型 id）。"""

    try:
        connection = service.create_connection(
            name=payload.name,
            base_url=payload.base_url,
            api_key=payload.api_key,
            models=[model.to_entry() for model in payload.models],
        )
    except Exception as exc:
        raise_configuration_error(exc)
    return ModelConnectionResponse.from_entry(connection)


@app.post(f"{_ROOT}/discover", response_model=ModelDiscoveryResponse)
async def discover_models(
    payload: ModelDiscoveryRequest,
    service: ModelDiscoveryService = Depends(get_model_discovery_service),
) -> ModelDiscoveryResponse:
    """发现远端模型 ID；远端失败时返回空列表而不打断配置表单。"""

    models = await service.discover(base_url=payload.base_url, api_key=payload.api_key)
    return ModelDiscoveryResponse(models=models)


@app.post(f"{_ROOT}/test")
async def test_draft_model(
    payload: ModelConfigTestRequest,
    service: ModelConfigService = Depends(get_model_config_service),
) -> dict[str, object | None]:
    """对一个尚未保存的「连接 + 模型」执行一次真实 OpenAI 兼容请求。"""

    result = await service.test_draft(
        base_url=payload.base_url, api_key=payload.api_key, model=payload.to_entry()
    )
    return {
        "success": result.success,
        "elapsed_ms": result.elapsed_ms,
        "error_code": result.error_code,
        "error_message": result.error_message,
    }


@app.put(f"{_ROOT}/{{connection_id}}", response_model=ModelConnectionResponse)
async def update_model_connection(
    connection_id: str,
    payload: ModelConnectionUpdateRequest,
    service: ModelConfigService = Depends(get_model_config_service),
) -> ModelConnectionResponse:
    """更新连接的显式字段（不传 ``api_key`` 即保留原 Key）。"""

    try:
        connection = service.update_connection(
            connection_id, **payload.model_dump(exclude_none=True)
        )
    except Exception as exc:
        raise_configuration_error(exc)
    return ModelConnectionResponse.from_entry(connection)


@app.delete(f"{_ROOT}/{{connection_id}}")
async def delete_model_connection(
    connection_id: str,
    service: ModelConfigService = Depends(get_model_config_service),
) -> dict[str, object]:
    """幂等删除连接及其全部模型。"""

    try:
        service.delete_connection(connection_id)
    except Exception as exc:
        raise_configuration_error(exc)
    return {"connection_id": connection_id, "deleted": True}


@app.post(f"{_ROOT}/{{connection_id}}/models", response_model=ModelConnectionResponse)
async def add_models(
    connection_id: str,
    payload: ModelConfigsAddRequest,
    service: ModelConfigService = Depends(get_model_config_service),
) -> ModelConnectionResponse:
    """给已有连接追加一批模型，返回写入后的连接。"""

    try:
        connection = service.add_models(
            connection_id, [model.to_entry() for model in payload.models]
        )
    except Exception as exc:
        raise_configuration_error(exc)
    return ModelConnectionResponse.from_entry(connection)


@app.put(
    f"{_ROOT}/{{connection_id}}/models/{{model_name:path}}", response_model=ModelConfigResponse
)
async def update_model(
    connection_id: str,
    model_name: str,
    payload: ModelConfigUpdateRequest,
    service: ModelConfigService = Depends(get_model_config_service),
) -> ModelConfigResponse:
    """更新模型的上下文窗口或能力声明。"""

    try:
        record = service.update_model(
            connection_id, model_name, **payload.model_dump(exclude_none=True)
        )
    except Exception as exc:
        raise_configuration_error(exc)
    return ModelConfigResponse.from_entry(record.connection, record.model)


@app.delete(f"{_ROOT}/{{connection_id}}/models/{{model_name:path}}")
async def delete_model(
    connection_id: str,
    model_name: str,
    service: ModelConfigService = Depends(get_model_config_service),
) -> dict[str, object]:
    """幂等删除一个模型。"""

    try:
        service.delete_model(connection_id, model_name)
    except Exception as exc:
        raise_configuration_error(exc)
    return {"config_id": f"{connection_id}/{model_name}", "deleted": True}
