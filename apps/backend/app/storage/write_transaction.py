"""主库写事务辅助。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session, sessionmaker


@contextmanager
def begin_immediate(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """开启一个 SQLite ``BEGIN IMMEDIATE`` 事务并在退出时提交或回滚。

    参数:
        session_factory: 绑定主库 engine 的 SQLAlchemy session 工厂。

    返回:
        已取得 SQLite 写锁的 Session。

    异常:
        任意异常: 回滚事务后继续向上传播。

    副作用:
        事务开始时取得 SQLite 写锁，阻止其它连接提交并发写入；调用方负责在事务提交后
        处理不属于主库事务的进程内资源清理。
    """

    with session_factory() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
