"""Terminal session persistence value object."""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.terminal_session_model import TerminalSessionModel
from app.utils.datetime_utils import from_text, to_text


@dataclass(frozen=True)
class TerminalSessionRecord:
    """持久化的终端 session 元数据。

    本记录只描述 backend 可恢复和诊断所需的 session 元数据，不包含 PTY handle、
    worker connection、输出 ring buffer 或 subscriber。那些状态只存在当前 backend
    进程内；backend 重启后 active session 会被收敛为 ``interrupted``。
    """

    session_id: str
    task_id: int
    workspace_id: int
    created_by_run_id: int | None
    initial_cwd: str
    shell_kind: str
    shell_executable: str
    worker_instance_id: str
    worker_pid: int | None
    status: str
    end_reason: str | None
    exit_code: int | None
    cols: int
    rows: int
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
            "created_by_run_id": self.created_by_run_id,
            "initial_cwd": self.initial_cwd,
            "shell_kind": self.shell_kind,
            "shell_executable": self.shell_executable,
            "worker_instance_id": self.worker_instance_id,
            "worker_pid": self.worker_pid,
            "status": self.status,
            "end_reason": self.end_reason,
            "exit_code": self.exit_code,
            "cols": self.cols,
            "rows": self.rows,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
            "last_activity_at": to_text(self.last_activity_at),
            "ended_at": to_text(self.ended_at) if self.ended_at else None,
        }

    @classmethod
    def from_model(cls, row: TerminalSessionModel) -> "TerminalSessionRecord":
        """从 SQLAlchemy model 构造持久化值对象。"""

        return cls(
            session_id=row.session_id,
            task_id=row.task_id,
            workspace_id=row.workspace_id,
            created_by_run_id=row.created_by_run_id,
            initial_cwd=row.initial_cwd,
            shell_kind=row.shell_kind,
            shell_executable=row.shell_executable,
            worker_instance_id=row.worker_instance_id,
            worker_pid=row.worker_pid,
            status=row.status,
            end_reason=row.end_reason,
            exit_code=row.exit_code,
            cols=row.cols,
            rows=row.rows,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            last_activity_at=from_text(row.last_activity_at),
            ended_at=from_text(row.ended_at) if row.ended_at else None,
        )
