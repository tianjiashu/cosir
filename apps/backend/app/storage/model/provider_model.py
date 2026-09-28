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
- ``name`` 是 ``llm_provider.json`` 的能力注册表名称（例如 ``deepseek``），允许
  多行配置复用；``provider_type`` 属性对应列名 ``type``，表示接入协议分类。
"""

from sqlalchemy import Boolean, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ProviderModel(StorageBase):
    """``providers`` 表：一个可独立路由的模型厂商配置实例。"""

    __tablename__ = "providers"
    __table_args__ = (
        # 同一能力类型允许配置多个实例，但用户可见名称必须可区分。
        Index("uq_providers_display_name", "display_name", unique=True),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider_type: Mapped[str] = mapped_column("type", Text, nullable=True)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
