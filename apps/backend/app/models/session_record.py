"""会话持久化状态值对象。

单一职责：承载一个会话的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/session_crud`` 负责）。

Session 不绑定 Agent：Agent 身份随入队输入走、在建 Run 时固化到 Run，``SessionRecord`` 只描述
Session 容器自身状态。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from app.models.session_extra import SessionExtra, SessionFork
from app.models.session_kind import SessionType
from app.storage.model.session_model import SessionModel
from app.utils.datetime_utils import from_text, to_text


@dataclass
class SessionRecord:
    """表示一个会话的持久化状态。"""

    id: int
    workspace_id: int
    title: str
    created_at: datetime
    updated_at: datetime
    extra: SessionExtra = field(default_factory=SessionExtra)
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    session_type: SessionType = SessionType.USER
    parent_session_id: int | None = None
    current_run_id: int | None = None
    # 派生深度（见 ``models.session_kind``）；创建后不变，所以缓存在运行时空间里的记录也可信。
    derivation_depth: int = 0

    @property
    def unstarted(self) -> bool:
        """未开始的会话：用户会话且从未有过 Run。

        新会话页在第一次需要 Session（开终端、开浏览器标签、发首条消息）时取或建它，每个工作区至多
        一个、反复复用（``WorkspaceService.get_or_create_unstarted_session``）；它不进侧栏，首个 Run
        建立后原地成为普通会话。只看当前 Run 指针即可：指针只在建 Run 时写入，Run 只随整个 Session
        一起删除，不会回到空。fork 继承了上下文、一建好就进侧栏，不算未开始。
        """
        return self.session_type == SessionType.USER and self.current_run_id is None

    def deriver_id(self) -> int | None:
        """派生出本 Session 的那个 Session（委派或 Agent fork 的发起方）；不是 Agent 派生的为 ``None``。

        ``send_message`` / ``session_status`` 的寻址只认这条父子关系。不能只看 ``parent_session_id``：
        Agent fork 不挂所有权子树，发起方记在 ``extra.fork``；用户手动 fork 的不算。
        """
        if self.session_type == SessionType.DELEGATED:
            return self.parent_session_id
        if self.session_type == SessionType.AGENT_FORK:
            return cast(SessionFork, self.extra.fork).source_session_id
        return None

    def derived_by(self, session_id: int) -> bool:
        """本 Session 是否由 ``session_id`` 的 Agent 派生。"""
        return self.deriver_id() == session_id

    def to_dict(self) -> dict[str, object]:
        """将会话状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            包含会话字段的字典；``current_run_id`` 与
            ``parent_session_id`` 可能为 None。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "extra": self.extra.model_dump(mode="json"),
            "session_type": self.session_type,
            "parent_session_id": self.parent_session_id,
            "current_run_id": self.current_run_id,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }

    @classmethod
    def from_model(cls, row: SessionModel) -> "SessionRecord":
        """从 ORM 行构造会话记录值对象。

        参数:
            row: ``sessions`` 表的 SQLAlchemy 行对象。

        返回:
            对应的 ``SessionRecord``；文本时间戳经 ``from_text`` 还原为 datetime。

        异常:
            无。

        副作用:
            无。
        """
        session_type = SessionType(row.session_type)
        extra = SessionExtra.model_validate(row.extra or {})
        extra.validate_session_type(session_type)
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            title=row.title,
            extra=extra,
            tool_schemas=row.tool_schemas or [],
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            session_type=session_type,
            parent_session_id=row.parent_session_id,
            current_run_id=row.current_run_id,
            derivation_depth=row.derivation_depth,
        )

    def to_model(self) -> SessionModel:
        """将 session 记录转换为 ORM 行。"""

        model_kwargs: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "title": self.title,
            "extra": self.extra.model_dump(mode="json"),
            "tool_schemas": self.tool_schemas or [],
            "session_type": self.session_type,
            "parent_session_id": self.parent_session_id,
            "current_run_id": self.current_run_id,
            "derivation_depth": self.derivation_depth,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }
        if self.id:
            model_kwargs["id"] = self.id
        return SessionModel(**model_kwargs)
