"""工具执行上下文值对象。

单一职责：承载一次工具执行所处的运行时边界——任务、工作区与其根路径。
不负责路径 containment 校验（由 ProjectPathResolver 负责）、也不负责
工作区记录的数据库查询（由 WorkspaceService 负责）。
"""

from dataclasses import dataclass
from pathlib import Path

from app.models.workspace_record import WorkspaceRecord


@dataclass(frozen=True)
class ToolExecutionContext:
    """一次工具执行所处的运行时边界（任务 / 工作区 / 工作区根路径 / 轮次）。"""

    task_id: str
    workspace_id: str
    workspace_root: Path
    turn_id: str = ""

    @classmethod
    def from_workspace(
        cls, task_id: str, workspace: WorkspaceRecord, turn_id: str = ""
    ) -> "ToolExecutionContext":
        """从工作区记录与任务标识构造执行上下文。

        参数:
            task_id: 当前执行所属的任务标识符。
            workspace: 解析出的工作区记录；其 ``root_path`` 即工具边界基准根。
            turn_id: 当前执行所属的轮次标识；用于把文件操作快照关联到具体 turn，
                供 task 级变更集（``change_set_service``）按文件精准还原。缺省为空
                字符串，表示未携带轮次上下文（如非 turn 驱动的一次性执行）。

        返回:
            绑定了该任务、工作区边界与轮次标识的 ToolExecutionContext。

        异常:
            无。

        副作用:
            无。
        """

        return cls(
            task_id=task_id,
            workspace_id=workspace.workspace_id,
            workspace_root=Path(workspace.root_path),
            turn_id=turn_id,
        )
