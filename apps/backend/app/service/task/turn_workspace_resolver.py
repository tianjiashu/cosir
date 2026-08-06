"""turn → task → workspace_path 的纯解析。

单一职责：给定一个 ``TurnRecord``，解析出它所属 task 的 workspace 根路径
（``workspace_path``），供 CodeGraph 索引准备使用。

职责边界：
- 负责：按 turn.task_id 取 task、按 task.workspace_id 取 workspace，返回 workspace.root_path。
- 不负责：索引准备（归 CodeGraphLifecycleService / CodeGraphIndexPrepareHook）、
  turn 状态管理（归 TurnService）、事件发布（归 workspace 创建即索引链路）。
"""

from app.models import TurnRecord
from app.service import depends as service_depends


class TurnWorkspaceResolver:
    """解析 turn 所属 workspace 根路径（API 层唯一解析点，见方案二 §4.2）。"""

    def __init__(self) -> None:
        """构造解析器（取得 task/workspace CRUD 单例）。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            无。
        """
        self._task = service_depends.get_task_crud()
        self._workspace = service_depends.get_workspace_crud()

    def resolve(self, turn: TurnRecord) -> str | None:
        """返回 turn 所属 workspace 根路径；解析失败返回 None。

        参数:
            turn: 待解析的轮次记录。

        返回:
            workspace 根路径字符串；task/workspace 不存在或缺失路径时返回 None
            （调用方据此跳过索引准备）。

        异常:
            KeyError: 当 task 或 workspace 不存在时抛出（由调用方决定处理）。

        副作用:
            读取 tasks / workspaces 表。
        """
        if turn.task_id is None or not turn.task_id:
            return None
        task = self._task.get(turn.task_id)
        if task.workspace_id is None or not task.workspace_id:
            return None
        workspace = self._workspace.get(task.workspace_id)
        root_path = workspace.root_path
        if not root_path:
            return None
        return root_path
