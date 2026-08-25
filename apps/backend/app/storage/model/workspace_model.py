from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class WorkspaceModel(StorageBase):
    """`workspaces` 表模型。"""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
