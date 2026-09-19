"""``models`` 表 ORM 模型（厂商下的可用模型条目）。

单一职责：承载一个模型条目（ModelEntry）的持久化结构，仅描述表结构。
CRUD 收口在 ``app.storage.crud.model_entry_crud``，值对象在
``app.models.model_entry_record``。

设计要点：
- ``model_name`` 为 Provider 使用的模型名（如 ``deepseek-chat``），
  同一厂商内唯一（``uq_models_provider_model_name``），是模型解析链按名查启用的主路径。
- ``max_context_window`` NOT NULL：DB 是模型唯一事实来源，窗口不允许落空；
  discover 导入时预填 Provider 目录中的已知值，用户可改。
- 随 provider 级联删除（``ON DELETE CASCADE``，引擎已开 foreign_keys）。
"""

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ModelEntryModel(StorageBase):
    """``models`` 表：某厂商下的一个可用模型条目。"""

    __tablename__ = "models"
    __table_args__ = (
        # 模型解析链按名查启用模型（resolver 主路径）。
        Index("idx_models_model_name", "model_name"),
        # 厂商内模型名唯一（路由名归属语义）。
        Index("uq_models_provider_model_name", "provider_id", "model_name", unique=True),
    )

    provider_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    max_context_window: Mapped[int] = mapped_column(Integer, nullable=False)
    supports_thinking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    supports_image: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    supports_video: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
