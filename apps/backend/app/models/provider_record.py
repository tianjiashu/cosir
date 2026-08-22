"""模型厂商持久化状态值对象。

单一职责：承载一个模型厂商（Provider）的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/provider_crud`` 负责）。

``api_key`` 直接承载 Key 明文（DB 唯一事实来源，本地 SQLite 明文存储）；
**明文仅允许在本值对象与运行时 ``LLMRuntimeConfig`` 之间流动**。序列化
（``to_dict``）刻意不输出该字段，避免 Key 明文进入日志 / 事件 / API 响应。
"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.provider_model import ProviderModel
from app.utils.datetime_utils import from_text, to_text


@dataclass
class ProviderRecord:
    """表示一个模型厂商配置行。"""

    provider_id: str
    name: str
    provider_type: str
    created_at: datetime
    updated_at: datetime
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool = True
    sort_order: int = 0

    def to_dict(self) -> dict[str, str | int | bool | None]:
        """将厂商状态转换为可序列化为 JSON 的字典。

        不输出 ``api_key`` 明文：``to_dict`` 面向日志 / 事件 / API 响应消费，
        明文仅由 service 层内部经属性访问取用。

        参数:
            无。

        返回:
            包含厂商字段的字典；键 ``type`` 对外契约名（与 ORM 列名一致）；
            不含 Key 明文。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "type": self.provider_type,
            "base_url": self.base_url,
            "enabled": self.enabled,
            "sort_order": self.sort_order,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }

    @classmethod
    def from_model(cls, row: ProviderModel) -> "ProviderRecord":
        """从 ORM 行构造厂商记录值对象。

        参数:
            row: ``providers`` 表的 SQLAlchemy 行对象。

        返回:
            对应的 ``ProviderRecord``；文本时间戳经 ``from_text`` 还原为 datetime。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            provider_id=row.provider_id,
            name=row.name,
            provider_type=row.provider_type,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            base_url=row.base_url,
            api_key=row.api_key,
            enabled=bool(row.enabled),
            sort_order=row.sort_order,
        )
