"""Run-scoped terminal metadata value object."""

from dataclasses import dataclass
from datetime import datetime

from app.utils.datetime_utils import to_text


@dataclass(frozen=True)
class TerminalSessionRecord:
    """后端进程内 terminal runtime 使用的可序列化元数据。

    该对象不是 SQLAlchemy 持久化模型。它只描述一个 Run 级终端的可序列化元数据；
    PTY handle、worker connection、输出 ring buffer 和 subscriber 只存在当前 backend
    进程内，并由 ``TerminalSessionService`` 管理。工作流需要时通过 ``to_dict`` 将
    allowlisted 字段投影进 LangGraph checkpoint。
    """

    session_id: str
    task_id: int
    workspace_id: int
    run_id: int
    initial_cwd: str
    shell_kind: str
    shell_executable: str
    worker_instance_id: str
    worker_pid: int | None
    status: str
    end_reason: str | None
    exit_code: int | None
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    ended_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        """转换为 API/tool 可使用的普通字典。"""

        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "initial_cwd": self.initial_cwd,
            "shell_kind": self.shell_kind,
            "shell_executable": self.shell_executable,
            "worker_instance_id": self.worker_instance_id,
            "worker_pid": self.worker_pid,
            "status": self.status,
            "end_reason": self.end_reason,
            "exit_code": self.exit_code,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
            "last_activity_at": to_text(self.last_activity_at),
            "ended_at": to_text(self.ended_at) if self.ended_at else None,
        }
