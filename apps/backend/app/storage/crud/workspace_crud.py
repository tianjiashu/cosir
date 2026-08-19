"""``workspaces`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``workspaces`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：workspace 单表读写、``WorkspaceModel``↔``WorkspaceRecord`` 转换。
- 不负责：删除工作区时的跨表级联清理（task / turn / run / trace，由 ``WorkspaceService``
  编排）、业务规则。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from uuid import uuid4

from sqlalchemy import asc, delete, select

from app.models import WorkspaceRecord
from app.storage.model.workspace_model import WorkspaceModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class WorkspaceCrud:
    """``workspaces`` 表的纯 CRUD。

    仅负责单表读写与 model↔record 转换，不承担跨表级联；所有方法通过共享主库
    session 工厂访问数据库。
    """

    def __init__(self) -> None:
        """绑定主库共享 session 工厂。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 ``init_storage`` 尚未调用（主库 session 工厂不可用）。

        副作用:
            无（仅复用已初始化的主库 session 工厂）。
        """
        self._session_factory = main_session_factory()

    def create(self, name: str, root_path: str) -> WorkspaceRecord:
        """新建一个工作区并落库。

        ``workspace_id`` 由本方法生成（UUID4），创建 / 更新时间取当前 UTC 时间；name 与
        root_path 会去除首尾空白后存储。

        参数:
            name: 工作区名称；不能为空白。
            root_path: 工作区根路径；不能为空白。

        返回:
            落库成功的 ``WorkspaceRecord``。

        异常:
            ValueError: 如果 name 或 root_path 去除首尾空白后为空。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``workspaces`` 表插入一行。
        """

        if not name.strip():
            raise ValueError("workspace name must not be blank")
        if not root_path.strip():
            raise ValueError("workspace root_path must not be blank")
        now = utc_now()
        workspace = WorkspaceRecord(str(uuid4()), name.strip(), root_path.strip(), now, now)
        with self._session_factory.begin() as session:
            session.add(
                WorkspaceModel(
                    workspace_id=workspace.workspace_id,
                    name=workspace.name,
                    root_path=workspace.root_path,
                    created_at=to_text(workspace.created_at),
                    updated_at=to_text(workspace.updated_at),
                )
            )
        return workspace

    def list_all(self) -> list[WorkspaceRecord]:
        """列出全部工作区，按创建时间升序。

        参数:
            无。

        返回:
            全部工作区列表，按 ``created_at`` 再 ``workspace_id`` 升序；无数据时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(WorkspaceModel).order_by(
                        asc(WorkspaceModel.created_at), asc(WorkspaceModel.workspace_id)
                    )
                )
                .scalars()
                .all()
            )
        return [WorkspaceRecord.from_model(row) for row in rows]

    def get(self, workspace_id: str) -> WorkspaceRecord:
        """按标识返回单个工作区。

        参数:
            workspace_id: 工作区标识。

        返回:
            匹配的 ``WorkspaceRecord``。

        异常:
            KeyError: 如果指定工作区不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row: WorkspaceModel | None = session.get(WorkspaceModel, workspace_id)
        if row is None:
            raise KeyError(workspace_id)
        return WorkspaceRecord.from_model(row)

    def delete(self, workspace_id: str) -> None:
        """删除单个工作区记录。

        仅删除 ``workspaces`` 表自身的行，不级联清理该工作区下的 task / turn / run / trace
        （级联由 ``WorkspaceService`` 编排）。

        参数:
            workspace_id: 工作区标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``workspaces`` 表删除匹配的行；工作区不存在时静默无操作。
        """

        with self._session_factory.begin() as session:
            session.execute(
                delete(WorkspaceModel).where(WorkspaceModel.workspace_id == workspace_id)
            )


