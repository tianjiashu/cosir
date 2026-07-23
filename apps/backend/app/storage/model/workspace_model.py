from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class WorkspaceModel(StorageBase):
    """`workspaces` 表模型。"""

    __tablename__ = "workspaces"

    workspace_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
