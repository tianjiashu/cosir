"""可选模型响应。"""

from pydantic import BaseModel

from app.models.model_config_file import ModelConnectionEntry, ModelEntry


class ModelConfigResponse(BaseModel):
    """连接下的一个模型；``config_id`` 是 Run、Agent 配置引用它用的「连接 id/模型名」。"""

    config_id: str
    connection_id: str
    model_name: str
    context_window_k: int
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_image: bool

    @classmethod
    def from_entry(
        cls, connection: ModelConnectionEntry, model: ModelEntry
    ) -> "ModelConfigResponse":
        return cls(
            config_id=connection.model_config_id(model.model_name),
            connection_id=connection.id,
            **model.model_dump(),
        )
