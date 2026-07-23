"""``workspaces`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``workspaces`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：workspace 单表读写、``WorkspaceModel``↔``WorkspaceRecord`` 转换。
- 不负责：删除工作区时的跨表级联清理（task / turn / run / trace，由 ``WorkspaceService``
  编排）、业务规则。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import asc, select

from app.models import WorkspaceRecord
from app.storage.model.workspace_model import WorkspaceModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import from_text, to_text


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

    def ensure_default(self, now: datetime) -> str:
        """确保默认工作区存在，返回其标识（幂等）。

        使用固定标识 ``"default-workspace"``：不存在则创建，已存在则直接返回，可安全重复调用。

        参数:
            now: 用于填充创建 / 更新时间的时间戳（由调用方提供，便于测试与时钟统一）。

        返回:
            默认工作区标识 ``"default-workspace"``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询或写入失败。

        副作用:
            默认工作区不存在时向 ``workspaces`` 表插入一行。
        """

        workspace_id = "default-workspace"
        with self._session_factory.begin() as session:
            if session.get(WorkspaceModel, workspace_id) is None:
                session.add(
                    WorkspaceModel(
                        workspace_id=workspace_id,
                        name="Default Workspace",
                        root_path=".",
                        created_at=to_text(now),
                        updated_at=to_text(now),
                    )
                )
        return workspace_id

    def create(self, name: str, root_path: str) -> WorkspaceRecord:
        """新建一个工作区并落库。

        ``workspace_id`` 由本方法生成（UUID4），创建 / 更新时间取本地当前时间；name 与
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
        now = datetime.now()
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
        return [self._workspace_from_model(row) for row in rows]

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
            row = session.get(WorkspaceModel, workspace_id)
        if row is None:
            raise KeyError(workspace_id)
        return self._workspace_from_model(row)

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

        from sqlalchemy import delete

        with self._session_factory.begin() as session:
            session.execute(
                delete(WorkspaceModel).where(WorkspaceModel.workspace_id == workspace_id)
            )

    def _workspace_from_model(self, row: WorkspaceModel) -> WorkspaceRecord:
        """把 ``WorkspaceModel`` ORM 行转换为业务 ``WorkspaceRecord``。

        转换过程把库中存储的文本时间戳还原为 datetime。

        参数:
            row: 查询得到的 ``WorkspaceModel`` 行。

        返回:
            对应的 ``WorkspaceRecord``。

        异常:
            无。

        副作用:
            无。
        """
        return WorkspaceRecord(
            row.workspace_id,
            row.name,
            row.root_path,
            from_text(row.created_at),
            from_text(row.updated_at),
        )
