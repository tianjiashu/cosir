"""模型连接配置 ORM 模型。

本模型只描述一个可独立路由的 OpenAI 兼容模型连接配置，不包含厂商注册表、模型目录
或网络测试逻辑。API Key 由本机 SQLite 保存为连接配置事实，序列化响应层不得回传明文。
"""

from sqlalchemy import Boolean, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class ModelConfigModel(StorageBase):
    """``model_configs`` 表：模型名称、连接地址、密钥和上下文窗口的持久化记录。"""

    __tablename__ = "model_configs"
    __table_args__ = (Index("uq_model_configs_config_name", "config_name", unique=True),)

    config_name: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    context_window_k: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
