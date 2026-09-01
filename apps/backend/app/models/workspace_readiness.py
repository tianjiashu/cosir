"""Workspace 准备结果值对象。

单一职责：承载 ``CodeGraphLifecycleService.ensure_ready`` 的返回结果——索引是否就绪、
采取了哪个动作、摘要与降级原因。不承载任何编排逻辑（编排归 LifecycleService）。后续
workspace 准备动作扩展时，本值对象可随准备流程演化。
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class WorkspaceReadiness:
    """workspace 准备结果。

    ``ready=False`` 表示 CodeGraph 不可用，调用方应降级到文件搜索（但不阻断任务）。
    """

    ready: bool
    state: str
    action_taken: str
    files_changed: int
    duration_ms: int
    degraded_reason: str | None
    workspace_id: int | None = None
    revision: int = 0
    updated_at: datetime | None = None

    @property
    def status(self) -> str:
        """返回对外 snapshot 契约使用的状态名称。

        参数:
            无。

        返回:
            与 ``state`` 相同的稳定状态字符串。

        异常:
            无。

        副作用:
            无。
        """

        return self.state

    @property
    def reason(self) -> str | None:
        """返回对外 snapshot 契约使用的降级原因。

        参数:
            无。

        返回:
            ``degraded_reason``；就绪或尚无原因时为 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        return self.degraded_reason

    @classmethod
    def initial(cls, workspace_id: int) -> "WorkspaceReadiness":
        """构造尚未执行准备流程的 workspace 初始快照。

        参数:
            workspace_id: 所属 workspace 标识。

        返回:
            ``pending`` 状态、revision 为 0 的初始快照。

        异常:
            无。

        副作用:
            无。
        """

        return cls(
            ready=False,
            state="pending",
            action_taken="none",
            files_changed=0,
            duration_ms=0,
            degraded_reason=None,
            workspace_id=workspace_id,
            revision=0,
        )
