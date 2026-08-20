"""``task`` 聚合根 / ``workspace`` 的原子级联删除。

单一职责：把「删除一个任务树或工作区及其全部子产物」做成单个 ``BEGIN IMMEDIATE``
写锁事务，保证原子性（全删或全不删）与并发安全（删除期间无并发写插入孤儿数据）。
本模块不承担普通单表读写——单表 CRUD 收口在 ``app.storage.crud``。

职责边界：
- 负责：跨多表（``runtime_events`` / ``turn_messages`` / ``file_snapshots`` /
  ``turns`` / ``delegations`` / ``tasks`` / ``workspaces``）的原子级联删除。
- 不负责：单表增删改查、业务规则校验、事件广播（由上层 service 编排）。

为什么不用各 CRUD 的 ``delete_by_ids`` 拼装：
单个 CRUD 的 ``delete_by_ids`` 各自开独立事务，组合后非原子；且中间任何一步失败会
留下半删状态、并发写可在删除期间插入孤儿数据。本模块用原生连接 +
``BEGIN IMMEDIATE``（对齐 ``DelegationCrud`` 已验证的范式）把整条删除链收进单一
临界区：SQLite 写锁使删除期间其它连接的写操作在 busy_timeout 内排队，根除并发孤儿。
"""

from sqlalchemy import delete, or_, select, update

from app.storage.model.delegation_model import DelegationModel
from app.storage.model.file_snapshot_model import FileSnapshotModel
from app.storage.model.runtime_event_model import RuntimeEventModel
from app.storage.model.task_model import TaskModel
from app.storage.model.turn_message_model import TurnMessageModel
from app.storage.model.turn_model import TurnModel
from app.storage.model.workspace_model import WorkspaceModel
from app.storage.store_engines import main_session_factory


class CascadeDeleter:
    """原子级联删除 ``task`` 聚合根与 ``workspace`` 及其全部子产物。

    构造时通过 ``main_session_factory()`` 取得主库共享引擎；必须在
    ``init_storage()`` 之后实例化，本类不创建、不释放引擎。所有删除均在单个
    ``BEGIN IMMEDIATE`` 事务内完成。
    """

    def __init__(self) -> None:
        """绑定主库共享引擎。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果主存储尚未初始化（主库引擎不可用）。

        副作用:
            无（仅复用已初始化的主库引擎，不创建 / 释放）。
        """
        self._engine = main_session_factory().kw["bind"]

    def delete_task_tree(self, root_task_id: str) -> int:
        """在单个 ``BEGIN IMMEDIATE`` 事务内原子删除任务树及其全部子产物。

        收集 ``root_task_id`` 所在的整棵任务树（含递归委派子任务），再收集其下全部
        turn 标识，在单个写锁事务内按外键依赖逆序删除
        ``turn_messages -> file_snapshots -> turns -> runtime_events -> delegations -> tasks``。
        ``runtime_events`` 的删除不依赖 turn 存在（按 turn_id 或 task_id 双键，见
        ``_delete_all_artifacts``）。``tasks`` 因存在自引用外键（``parent_task_id``），
        采用「由叶子向根分层删除」规避单条 ``IN`` 全删时父行先于子行被删导致的 FK 冲突。

        参数:
            root_task_id: 待删除任务树的根任务标识。

        返回:
            删除的 task 行数（含根及其全部子任务）。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果事务执行失败（事务自动回滚）。

        副作用:
            从 ``runtime_events`` / ``turn_messages`` / ``file_snapshots`` /
            ``turns`` / ``delegations`` / ``tasks`` 表删除该任务树相关数据。
        """
        with self._engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                task_ids = self._collect_task_tree_ids(conn, root_task_id)
                if not task_ids:
                    conn.commit()
                    return 0
                self._delete_all_artifacts(conn, set(task_ids))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return len(task_ids)

    def delete_workspace(self, workspace_id: str) -> None:
        """在单个 ``BEGIN IMMEDIATE`` 事务内原子删除工作区及其下所有任务。

        先收集该工作区全部任务标识（含用户根任务与委派子任务），执行与
        ``delete_task_tree`` 相同的原子级联删除，最后删除工作区自身；全部在一个写锁
        事务内完成。

        参数:
            workspace_id: 待删除的工作区标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果事务执行失败（事务自动回滚）。

        副作用:
            从 ``runtime_events`` / ``turn_messages`` / ``file_snapshots`` /
            ``turns`` / ``delegations`` / ``tasks`` / ``workspaces`` 表删除该工作区
            相关数据。
        """
        with self._engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                workspace_task_ids = self._collect_workspace_task_ids(conn, workspace_id)
                self._delete_all_artifacts(conn, set(workspace_task_ids))
                conn.execute(
                    delete(WorkspaceModel).where(
                        WorkspaceModel.workspace_id == workspace_id
                    )
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _delete_all_artifacts(self, conn, task_ids: set[str]) -> None:
        """在给定连接上按外键依赖逆序删除一批 task 的全部子产物与 task 自身。

        ``tasks`` 与 ``turns`` 存在双向外键环（``turns.task_id -> tasks.task_id`` 与
        ``tasks.parent_turn_id -> turns.turn_id``），删除顺序必须先解除 ``tasks`` 对
        ``turns`` 的引用（把本批 ``tasks.parent_turn_id`` 置 NULL），再删 turns；否则
        SQLite 即时外键检查会在删 turns 时因 ``tasks.parent_turn_id`` 引用而报
        ``FOREIGN KEY constraint failed``。其余子产物按被引用方向先删。

        参数:
            conn: 当前写锁事务的原生连接。
            task_ids: 待删除的任务标识集合（含整棵任务树）。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果任一删除失败。

        副作用:
            删除 ``turn_messages`` / ``file_snapshots`` / ``turns`` / ``runtime_events`` /
            ``delegations`` / ``tasks`` 相关行；``task_ids`` 为空时不操作。其中
            ``runtime_events`` 按 turn_id 或 task_id 双键删除，且不依赖 turn 存在。
        """
        if not task_ids:
            return
        # 解除 tasks.parent_turn_id -> turns 的引用（双向外键环，删 turns 前必须置空）。
        conn.execute(
            update(TaskModel)
            .where(TaskModel.task_id.in_(task_ids))
            .values(parent_turn_id=None)
        )
        turn_ids = self._collect_turn_ids(conn, task_ids)
        if turn_ids:
            conn.execute(
                delete(TurnMessageModel).where(TurnMessageModel.turn_id.in_(turn_ids))
            )
            conn.execute(
                delete(FileSnapshotModel).where(FileSnapshotModel.turn_id.in_(turn_ids))
            )
            conn.execute(delete(TurnModel).where(TurnModel.task_id.in_(task_ids)))
        # runtime_events 删除不依赖 turn 存在（turn_id 可空，允许 task 级全局事件在无
        # turn 时存在），故必须独立于 ``if turn_ids:`` 之外无条件执行；按 turn_id 或
        # task_id 双键覆盖，确保任务树内无论有无 turn 都不残留事件。
        conn.execute(
            delete(RuntimeEventModel).where(
                or_(
                    RuntimeEventModel.turn_id.in_(turn_ids),
                    RuntimeEventModel.task_id.in_(task_ids),
                )
            )
        )
        conn.execute(delete(DelegationModel).where(DelegationModel.task_id.in_(task_ids)))
        self._delete_task_tree_layered(conn, task_ids)

    @staticmethod
    def _collect_task_tree_ids(conn, root_task_id: str) -> list[str]:
        """在给定连接上广度优先收集 ``root_task_id`` 所在任务树的全部 task 标识。

        参数:
            conn: 当前写锁事务的原生连接。
            root_task_id: 根任务标识。

        返回:
            含根及其全部递归子任务的任务标识列表；根不存在时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            无（仅在该连接的当前事务内执行查询）。
        """
        collected: list[str] = []
        frontier = [root_task_id]
        while frontier:
            rows = conn.execute(
                select(TaskModel.task_id).where(TaskModel.task_id.in_(frontier))
            ).all()
            if not rows:
                break
            collected_ids = [row[0] for row in rows]
            collected.extend(collected_ids)
            child_rows = conn.execute(
                select(TaskModel.task_id).where(
                    TaskModel.parent_task_id.in_(collected_ids)
                )
            ).all()
            frontier = [row[0] for row in child_rows]
        return collected

    @staticmethod
    def _collect_turn_ids(conn, task_ids: set[str]) -> list[str]:
        """在给定连接上收集一批 task 下全部 turn 标识。

        参数:
            conn: 当前写锁事务的原生连接。
            task_ids: 待收集 turn 的任务标识集合。

        返回:
            这些 task 下的全部 turn 标识列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            无（仅在该连接的当前事务内执行一次查询）。
        """
        rows = conn.execute(
            select(TurnModel.turn_id).where(TurnModel.task_id.in_(task_ids))
        ).all()
        return [row[0] for row in rows]

    @staticmethod
    def _collect_workspace_task_ids(conn, workspace_id: str) -> list[str]:
        """在给定连接上收集某工作区下全部任务标识。

        不区分 ``task_type``（含用户根任务与委派子任务），删除工作区即清空其下所有
        task；层级关系不在此处理，由 ``_delete_task_tree_layered`` 分层删除规避
        自引用外键。

        参数:
            conn: 当前写锁事务的原生连接。
            workspace_id: 工作区标识。

        返回:
            该工作区的全部任务标识列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            无（仅在该连接的当前事务内执行一次查询）。
        """
        rows = conn.execute(
            select(TaskModel.task_id).where(TaskModel.workspace_id == workspace_id)
        ).all()
        return [row[0] for row in rows]

    @staticmethod
    def _delete_task_tree_layered(conn, task_ids: set[str]) -> None:
        """在给定连接上对任务树分层删除（由叶子向根），规避自引用外键。

        单条 ``DELETE ... WHERE task_id IN (...)"`` 无法保证父行在子行之后被删，
        SQLite 即时外键检查会对先删父行的自引用报错。因此每次只删除当前剩余集合中
        「不再被本批任何任务当作父节点」的叶子：查询 ``parent_task_id IN remaining``
        得到被引用为父的 task_id（这些父还有子任务在批内，暂不能删），
        ``leaves = remaining - 被引用为父的`` 即为当前叶子，先删；循环直到删光。

        参数:
            conn: 当前写锁事务的原生连接。
            task_ids: 待删除的任务标识集合（含整棵树）。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            在该连接的当前事务内逐层删除 ``tasks`` 行，直至 ``task_ids`` 清空。
        """
        remaining = set(task_ids)
        while remaining:
            child_refs = conn.execute(
                select(TaskModel.parent_task_id).where(
                    TaskModel.parent_task_id.in_(remaining)
                )
            ).all()
            referenced_as_parent = {row[0] for row in child_refs}
            leaves = remaining - referenced_as_parent
            conn.execute(delete(TaskModel).where(TaskModel.task_id.in_(leaves)))
            remaining -= leaves
