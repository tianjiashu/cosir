"""能交接新 Run 的输入（用户输入与 Agent 消息）共用的发起配置。

Run 期间锁定；只在消息交接新 Run 时生效，并入运行中的 Run 或唤醒挂起的 Run 时不看它。
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from app.models.enums.reasoning_effort import ReasoningEffort
from app.models.terminal_permissions import TerminalPermissionMode

if TYPE_CHECKING:
    from app.models.run.run_record import RunRecord
    from app.models.session.session_record import SessionRecord


class RunLaunch(BaseModel):
    """一个 Run 的发起配置；活动 Run 期间锁定，交接新 Run 与入队校验共用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # 交接新 Run 时的 Agent 身份；Session 不绑定 Agent，身份随发起配置走。
    agent_id: str = "main_agent"
    model_config_id: str
    reasoning_effort: ReasoningEffort | None = None
    terminal_permission_mode: TerminalPermissionMode = "request_approval"

    @classmethod
    def from_run(cls, run: "RunRecord | None") -> "RunLaunch | None":
        """沿用某个 Run 已冻结的发起配置；Run 没记录 Agent 或模型时返回 ``None``。

        模型被删后 Run 仍保留原 id，这里照样返回；能否开新 Run 由 ``SessionActor`` 查模型是否还在。
        """

        if run is None or run.agent_id is None or run.model_config_id is None:
            return None
        return cls(
            agent_id=run.agent_id,
            model_config_id=run.model_config_id,
            reasoning_effort=run.reasoning_effort,
            terminal_permission_mode=run.terminal_permission_mode,
        )

    @classmethod
    def from_fork(cls, session: "SessionRecord") -> "RunLaunch | None":
        """分支还没有本地 Run 时沿用分叉时冻结的初始配置；不依赖来源 Session 是否还在。

        冻结的模型 id 只是来历，不保证还存在：入队前的消息准备会校验所选模型，用户也可以换。
        不是分支，或冻结配置缺 Agent / 模型（已被删除）时返回 ``None``。
        """
        fork = session.extra.fork
        if fork is None:
            return None
        launch = fork.initial_launch
        if launch.agent_id is None or launch.model_config_id is None:
            return None
        return cls(
            agent_id=launch.agent_id,
            model_config_id=launch.model_config_id,
            reasoning_effort=launch.reasoning_effort,
            terminal_permission_mode=launch.terminal_permission_mode,
        )
