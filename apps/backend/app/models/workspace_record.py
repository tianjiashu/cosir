"""工作区持久化状态值对象。

单一职责：承载一个本地工作区的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/workspace_crud`` 负责）。
"""

from dataclasses import dataclass
from datetime import datetime

from app.utils.datetime_utils import to_text


@dataclass
class WorkspaceRecord:
    """表示一个本地工作区。"""

    workspace_id: str
    name: str
    root_path: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, str]:
        """将工作区状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            包含工作区字段的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "root_path": self.root_path,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }
