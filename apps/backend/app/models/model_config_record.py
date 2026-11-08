"""模型连接配置及用户声明能力的领域值对象。"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.model_config_model import ModelConfigModel
from app.utils.datetime_utils import from_text


@dataclass
class ModelConfigRecord:
    """表示一个可独立选择和调用的 OpenAI 兼容模型配置。

    API Key 只在本对象和模型构建/连通性测试的内部调用链中流动；``to_dict`` 明确不输出
    明文 Key，避免日志、事件或 HTTP 响应误泄露。
    """

    config_name: str
    base_url: str
    api_key: str
    model_name: str
    context_window_k: int
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    supports_image: bool = False
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    enabled: bool = True
    sort_order: int = 0

    def to_dict(self) -> dict[str, str | int | bool | None]:
        """返回不含 API Key 的可序列化配置摘要。"""

        return {
            "id": self.id,
            "config_name": self.config_name,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "context_window_k": self.context_window_k,
            "supports_thinking": self.supports_thinking,
            "supports_reasoning_effort": self.supports_reasoning_effort,
            "supports_image": self.supports_image,
            "enabled": self.enabled,
            "sort_order": self.sort_order,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_model(cls, row: ModelConfigModel) -> "ModelConfigRecord":
        """从 ``model_configs`` ORM 行构造领域记录。"""

        return cls(
            id=row.id,
            config_name=row.config_name,
            base_url=row.base_url,
            api_key=row.api_key,
            model_name=row.model_name,
            context_window_k=row.context_window_k,
            supports_thinking=bool(row.supports_thinking),
            supports_reasoning_effort=bool(row.supports_reasoning_effort),
            supports_image=bool(row.supports_image),
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            enabled=bool(row.enabled),
            sort_order=row.sort_order,
        )

    def to_model(self) -> ModelConfigModel:
        """将领域记录转换为 ORM 行。"""

        return ModelConfigModel(
            id=self.id,
            config_name=self.config_name,
            base_url=self.base_url,
            api_key=self.api_key,
            model_name=self.model_name,
            context_window_k=self.context_window_k,
            supports_thinking=self.supports_thinking,
            supports_reasoning_effort=self.supports_reasoning_effort,
            supports_image=self.supports_image,
            enabled=self.enabled,
            sort_order=self.sort_order,
        )
