"""模型连接与模型配置文件的响应。"""

from pydantic import BaseModel

from app.api.schemas.response.ModelConfigResponse import ModelConfigResponse
from app.models.model_config_file import ModelConfigFile, ModelConnectionEntry


class ModelConnectionResponse(BaseModel):
    """一个连接及其下模型（按文件顺序）；模型选择目录与配置管理都只读这一份。

    返回 API Key 供本机编辑表单回显（前端默认密码态）；Key 不进入日志、事件或领域摘要。
    """

    connection_id: str
    name: str
    base_url: str
    api_key: str
    models: list[ModelConfigResponse]

    @classmethod
    def from_entry(cls, connection: ModelConnectionEntry) -> "ModelConnectionResponse":
        return cls(
            connection_id=connection.id,
            name=connection.name,
            base_url=connection.base_url,
            api_key=connection.api_key,
            models=[
                ModelConfigResponse.from_entry(connection, model) for model in connection.models
            ],
        )


class ModelConfigFileResponse(BaseModel):
    """整个模型配置文件：文件路径（设置页展示，供用户直接编辑）与全部连接。"""

    path: str
    connections: list[ModelConnectionResponse]

    @classmethod
    def from_file(cls, path: str, document: ModelConfigFile) -> "ModelConfigFileResponse":
        return cls(
            path=path,
            connections=[ModelConnectionResponse.from_entry(c) for c in document.connections],
        )
