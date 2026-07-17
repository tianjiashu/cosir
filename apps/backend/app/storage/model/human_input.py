"""人工输入 SQLAlchemy model。"""

from sqlalchemy import Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class HumanInputRequestModel(StorageBase):
    """`human_input_requests` 表模型。"""

    __tablename__ = "human_input_requests"

    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    step_id: Mapped[str | None] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    schema_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    responded_at: Mapped[str | None] = mapped_column(Text)


class HumanInputResponseModel(StorageBase):
    """`human_input_responses` 表模型。"""

    __tablename__ = "human_input_responses"
    __table_args__ = (
        UniqueConstraint("request_id"),
        UniqueConstraint("idempotency_key"),
    )

    response_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
