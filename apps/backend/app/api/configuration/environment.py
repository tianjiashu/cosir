"""环境变量配置的 HTTP 路由。

负责 ``/configuration/environment`` 的读取与写入：读取返回脱敏后的字段视图，写入把请求的变更
集合转换为 service 的变更对象并落盘。白名单 schema、来源解析、敏感字段脱敏与 ``.env.local``
override 由 ``EnvironmentConfigurationService`` 承担，本模块不直接读写文件。
"""

from __future__ import annotations

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.request.EnvironmentUpdateRequest import EnvironmentUpdateRequest
from app.api.schemas.response.EnvironmentResponse import EnvironmentResponse
from app.app import app
from app.service.configuration.environment_configuration_service import (
    EnvironmentChange,
    EnvironmentConfigurationService,
)


@app.get("/configuration/environment", response_model=EnvironmentResponse)
async def get_environment_configuration() -> EnvironmentResponse:
    try:
        service = EnvironmentConfigurationService()
        return EnvironmentResponse(fields=service.read())
    except Exception as exc:
        raise_configuration_error(exc)


@app.put("/configuration/environment", response_model=EnvironmentResponse)
async def update_environment_configuration(
    payload: EnvironmentUpdateRequest,
) -> EnvironmentResponse:
    try:
        changes = {
            name: EnvironmentChange(operation=item.operation, value=item.value)
            for name, item in payload.changes.items()
        }
        service = EnvironmentConfigurationService()
        return EnvironmentResponse(
            fields=service.update(
                changes,
                reload_after_write=True,
            ),
        )
    except Exception as exc:
        raise_configuration_error(exc)
