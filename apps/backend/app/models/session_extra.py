"""Session 的持久化附属数据：分叉来源与派生调用，不承载执行生命周期。"""

from pydantic import BaseModel, ConfigDict

from app.models.enums.reasoning_effort import ReasoningEffort
from app.models.run_record import RunRecord
from app.models.session_kind import FORK_SESSION_TYPES, SessionType

# Agent 派生出的 Session：记下派生它的那次工具调用（撤回那一回合时据此收回）。
_AGENT_DERIVED_TYPES = frozenset({SessionType.DELEGATED, SessionType.AGENT_FORK})


class SessionForkLaunch(BaseModel):
    """分支的初始配置；已删除的 Agent/模型身份可为空，用户重新选择后仍可继续。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str | None
    model_config_id: str | None
    reasoning_effort: ReasoningEffort | None

    @classmethod
    def from_run(cls, run: RunRecord) -> "SessionForkLaunch":
        """冻结某个 Run 的发起配置作为分支的初始配置。"""

        return cls(
            agent_id=run.agent_id,
            model_config_id=run.model_config_id,
            reasoning_effort=run.reasoning_effort,
        )


class SessionFork(BaseModel):
    """不可变分叉来源与可改挂的侧栏关系；创建方式只由 SessionType 表达。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_session_id: int
    source_title: str
    tree_parent_session_id: int | None
    initial_launch: SessionForkLaunch


class SessionExtra(BaseModel):
    """现有 Session JSON 列的严格结构。普通与委派 Session 使用空对象，不开放任意扩展键。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fork: SessionFork | None = None
    # 派生出本 Session 的那次调用（委派或 Agent fork 在发起方 Session 里的 ``tool_call_id``）；发起方是
    # ``SessionRecord.deriver_id()``。撤回发起方的一个回合时，按它找出这一回合派生的 Session 一并收回。
    deriving_call_id: str | None = None
    # 可写根：相对工作区根的 POSIX 路径，``None`` 为整个工作区。创建时定、之后不变；派生时只能收窄，
    # 手动分叉原样继承（规则与消费方见 ``core.permission.writable_root``）。
    writable_root: str | None = None

    def validate_session_type(self, session_type: SessionType) -> None:
        """在持久化读取边界一次校验类型与附属数据，错误数据直接失败。"""
        if (self.fork is not None) != (session_type in FORK_SESSION_TYPES):
            raise ValueError(f"{session_type} carries invalid fork metadata")
        # 只有 Agent 派生出的 Session 能带；委派与 Agent fork 两个入口总会写上。
        if self.deriving_call_id is not None and session_type not in _AGENT_DERIVED_TYPES:
            raise ValueError(f"{session_type} carries invalid deriving call metadata")
        # 用户新建的会话恒为整个工作区；可写根只来自派生或从已收窄的会话分叉。
        if self.writable_root is not None and session_type is SessionType.USER:
            raise ValueError(f"{session_type} carries invalid writable root")
