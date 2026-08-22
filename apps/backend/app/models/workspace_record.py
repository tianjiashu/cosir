"""工作区持久化状态值对象。

单一职责：承载一个本地工作区的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/workspace_crud`` 负责）。
"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.workspace_model import WorkspaceModel
from app.utils.datetime_utils import from_text, to_text


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

    @classmethod
    def from_model(cls, row: WorkspaceModel) -> "WorkspaceRecord":
        """从 ORM 行构造工作区记录值对象。

        参数:
            row: ``workspaces`` 表的 SQLAlchemy 行对象。

        返回:
            对应的 ``WorkspaceRecord``；文本时间戳经 ``from_text`` 还原为 datetime。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            row.workspace_id,
            row.name,
            row.root_path,
            from_text(row.created_at),
            from_text(row.updated_at),
        )
