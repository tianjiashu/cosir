"""Conversation Session Context 持久化模型。"""

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase

AWAITING_STATUS_CONDITION = "json_extract(metadata_json, '$.status') = 'pending'"
"""部分索引 ``idx_session_context_awaiting`` 的条件；查询必须逐字使用它（见
``SessionContextCrud.awaiting_user``）。只有等待占位与后台占位会以 ``pending`` 落库，
索引因此只收录当下在等的极少数行，流式改写其他行不维护索引条目。"""


class SessionContextModel(StorageBase):
    """``session_contexts`` 表：会话级上下文单条消息的持久化事实。

    一行存储一条 SDK 请求消息 JSON 正文及按角色约束的 metadata，
    配合 ``sequence`` 维持跨 run 的全局插入顺序，``include_in_context``
    标记该消息是否纳入上下文视图；``project_to_transport`` 独立标记是否向前端投影。``turn`` 是这一行
    所属回合的 Session 内序号，写入时由 ``RuntimeMessageContextManager`` 一处判定（系统提示词为 0）。

    该表是上下文消息的**唯一持久化真相**，与
    :class:`app.models.session_context.SessionContextRecord` 一一对应；
    所有行↔对象映射必须经 record 的 ``_from_model`` / ``_to_model``。

    职责边界：
    - 负责：单条上下文消息的落盘（session_id、所属 run_id、消息 JSON、纳入标记、排序）。
    - 不负责：消息内容语义校验、上下文压缩策略（由上下文管理器负责）。
    """

    __tablename__ = "session_contexts"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_session_context_session_seq"),
        Index("idx_session_context_session_run", "session_id", "run_id"),
        # 覆盖计数查询用到的全部列：查询不必回表，规划器才稳定选它；只按键选时它与
        # ``idx_session_context_session_run`` 代价相同，按 Session 计数会落到后者、扫这个 Session 的全部行。
        # 只收录 pending 行，带上 metadata 也只占极少空间。
        Index(
            "idx_session_context_awaiting",
            "session_id",
            "run_id",
            "include_in_context",
            "metadata_json",
            sqlite_where=text(AWAITING_STATUS_CONDITION),
        ),
    )

    session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    message_json: Mapped[str] = mapped_column(Text, nullable=False)
    include_in_context: Mapped[bool] = mapped_column(default=True, nullable=False)
    # 模型上下文成员身份与前端可见性是独立事实。
    project_to_transport: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # 回合是写入时的事实，不从 ``run_id`` / ``project_to_transport`` 推导：fork 复制会改写这两项，
    # 推导出的边界因此会漂移；回合号随行复制，分支与来源的回合划分一致。
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
