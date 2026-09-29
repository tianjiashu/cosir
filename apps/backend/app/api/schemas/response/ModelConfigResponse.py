"""模型连接配置响应。"""

from pydantic import BaseModel

from app.models.model_config_record import ModelConfigRecord


class ModelConfigResponse(BaseModel):
    """返回本机模型配置及编辑所需的 API Key。

    该响应只在本机桌面应用的 HTTP 边界内使用。API Key 不进入日志、事件或
    ``ModelConfigRecord.to_dict``；前端收到后默认以密码态展示，仅用于配置编辑。
    """

    config_id: int
    config_name: str
    base_url: str
    api_key: str
    model_name: str
    context_window_k: int
    api_key_configured: bool
    enabled: bool
    sort_order: int
    created_at: str
    updated_at: str
    supports_thinking: bool = False
    supports_image: bool = False
    supports_reasoning_effort: bool = False

    @classmethod
    def from_record(cls, record: ModelConfigRecord) -> "ModelConfigResponse":
        """从领域配置记录构造 HTTP 响应。"""

        if record.id is None:
            raise ValueError("ModelConfigRecord.id 不能为空")
        return cls(
            config_id=record.id,
            config_name=record.config_name,
            base_url=record.base_url,
            api_key=record.api_key,
            model_name=record.model_name,
            context_window_k=record.context_window_k,
            api_key_configured=bool(record.api_key),
            enabled=record.enabled,
            sort_order=record.sort_order,
            created_at=record.created_at.isoformat() if record.created_at else "",
            updated_at=record.updated_at.isoformat() if record.updated_at else "",
            supports_thinking=record.supports_thinking,
            supports_image=record.supports_image,
            supports_reasoning_effort=record.supports_reasoning_effort,
        )
