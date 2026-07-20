"""日志数据库 SQLAlchemy model。"""

from sqlalchemy import Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class LogEntryModel(StorageBase):
    """`log_entries` 表模型。

    参数:
        无。

    返回:
        SQLAlchemy ORM 映射类。

    异常:
        无。

    副作用:
        注册 `log_entries` 表到统一 metadata。
    """

    __tablename__ = "log_entries"
    __table_args__ = (
        Index("idx_log_entries_trace_ts", "trace_id", "ts"),
        Index("idx_log_entries_level_ts", "level", "ts"),
        Index("idx_log_entries_event_ts", "event", "ts"),
        Index("idx_log_entries_ts", "ts"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    logger: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(Text)
    caller: Mapped[str | None] = mapped_column(Text)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    msg: Mapped[str] = mapped_column(Text, nullable=False)
    data_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'{}'"))
    error_json: Mapped[str | None] = mapped_column(Text)
    truncated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
