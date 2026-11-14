from pydantic import BaseModel, Field

from app.models.session_kind import SessionType
from app.models.session_record import SessionRecord
from app.utils.datetime_utils import to_text


class SessionResponseFork(BaseModel):
    """侧栏只读取树关系；来源和初始配置由对话 snapshot 提供。"""

    tree_parent_session_id: int | None


class SessionResponseExtra(BaseModel):
    """Session 附属数据的 UI 投影。"""

    fork: SessionResponseFork | None = None


class SessionResponse(BaseModel):
    """校验并序列化会话状态响应。

    Session 不绑定 Agent：本响应不含 agent_id，Agent 身份固化在各 Run 上。不下发 ``current_run_id``
    （后端的当前 Run 指针，对话界面的 Run 身份只在 snapshot 里供诊断），也不下发执行状态：
    界面的 Session 状态只来自 ``GET /session-feed/stream``，不在这里留第二个读取口。

    参数:
        session_id: 会话标识。
        workspace_id: 所属工作区标识。
        title: 会话标题。
        session_type: 会话类型，取值见 ``models.session_kind.SESSION_KINDS``：``"user"`` 为用户创建，
            ``"user_fork"`` / ``"agent_fork"`` 区分分叉发起方，``"delegated"`` 为委派子会话。
        parent_session_id: 父会话标识，委派子会话有值。
        unstarted: 未开始的会话（``SessionRecord.unstarted``）：前端目录据此不把它放进侧栏、把它当作
            所在工作区的新会话页。
        created_at: 创建时间文本。
        updated_at: 更新时间文本。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    session_id: int
    workspace_id: int
    title: str
    extra: SessionResponseExtra = Field(default_factory=SessionResponseExtra)
    session_type: SessionType = SessionType.USER
    parent_session_id: int | None = None
    unstarted: bool = False
    created_at: str
    updated_at: str

    @classmethod
    def from_record(
        cls,
        record: SessionRecord,
    ) -> "SessionResponse":
        """从 ``SessionRecord`` 值对象构造响应模型。

        把领域值对象前向映射为 API 响应模型，避免 ``**to_dict()`` 因字典值类型
        被推断为 ``str | None`` 而与必填 ``str`` 字段冲突（mypy 报错），同时消除
        重复的字段拆解逻辑。

        参数:
            record: 待转换的会话记录。

        返回:
            与记录字段对齐的 ``SessionResponse`` 实例。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            session_id=record.id,
            workspace_id=record.workspace_id,
            title=record.title,
            extra=SessionResponseExtra(fork=(
                SessionResponseFork(tree_parent_session_id=record.extra.fork.tree_parent_session_id)
                if record.extra.fork is not None else None
            )),
            session_type=record.session_type,
            parent_session_id=record.parent_session_id,
            unstarted=record.unstarted,
            created_at=to_text(record.created_at),
            updated_at=to_text(record.updated_at),
        )
