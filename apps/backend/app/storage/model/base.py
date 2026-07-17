"""SQLAlchemy model 基类。"""

from sqlalchemy.orm import DeclarativeBase


class StorageBase(DeclarativeBase):
    """项目自有数据库表的 SQLAlchemy Declarative 基类。

    参数:
        无。

    返回:
        SQLAlchemy Declarative Base 子类。

    异常:
        无。

    副作用:
        持有统一 metadata，供 schema 初始化和 CRUD 模块复用。
    """
