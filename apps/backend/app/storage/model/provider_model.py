"""``providers`` 表 ORM 模型（模型厂商配置）。

单一职责：承载一个模型厂商（Provider）的持久化结构，仅描述表结构。
CRUD 收口在 ``app.storage.crud.provider_crud``，值对象在
``app.models.provider_record``。

设计要点：
- 不预置内置厂商：首次使用经配置中心引导添加，「无厂商报错」语义依赖
  空表状态触发。
- ``api_key`` 为唯一事实来源，直接承载 Key 明文（用户已拍板：本地单机
  SQLite 明文存储，不做加密）；``init_schema`` 的「加列不删列」机制负责
  存量库补齐该列。运行时经 ``LLMRuntimeConfig`` 透传到
  ``factory.build_chat_model``，本表是 Key 唯一落点。
- ``provider_type`` 属性对应列名 ``type``（避免属性名遮蔽 Python 内置
  ``type``），取 ``deepseek`` / ``openai-compatible`` / ``anthropic`` /
  ``ollama`` / ``custom`` 之一，决定 litellm 前缀与默认 base_url。
"""

from sqlalchemy import Boolean, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ProviderModel(StorageBase):
    """``providers`` 表：模型厂商配置行。"""

    __tablename__ = "providers"

    provider_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    provider_type: Mapped[str] = mapped_column("type", Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
