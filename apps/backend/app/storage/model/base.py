"""SQLAlchemy 声明基类：统一承载所有实体的公共列与自动填充。

所有 ORM 实体继承 ``StorageBase``。基类自动提供三类公共列，业务模型无需重复声明：
- ``id``：自增整数 surrogate 主键（``Integer``、``primary_key``、``autoincrement``），
  是表内关联、级联删除与对外查询的唯一主键；业务外键列（如 ``task_id`` / ``run_id``）
  为 ``int`` 类型并指向关联表的 ``id``，由子类自行声明。
- ``created_at`` / ``updated_at``：ISO-8601 文本时间戳（项目约定时间戳以 ``Text``
  存储，不依赖数据库函数）。``created_at`` 在插入时默认当前 UTC；``updated_at`` 在
  插入与每次更新时刷新当前 UTC，均由应用层 Python 侧生成，保证跨数据库一致。

模块刻意保持 leaf 层：仅依赖标准库、SQLAlchemy 与 ``app.utils.datetime_utils``。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.utils.datetime_utils import to_text, utc_now


class StorageBase(DeclarativeBase):
    """ORM 声明基类：所有存储实体的公共祖先，提供自增 id 与时间戳自动填充。

    子类只需声明自身业务列；``id`` / ``created_at`` / ``updated_at`` 由基类统一
    管理，避免每个模型重复定义并手动填充时间戳。``id`` 为自增整数主键（SQLite 仅
    对 ``INTEGER PRIMARY KEY`` 生效 AUTOINCREMENT，故用 ``Integer`` 而非 ``BigInteger``，
    并启用 ``sqlite_autoincrement``）；各业务外键列（如 ``task_id`` / ``run_id``）为
    ``int`` 类型，由子类声明 ``ForeignKey`` 指向关联表的 ``id``。
    """

    __table_args__: Any = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    created_at: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=lambda: to_text(utc_now()),
    )
    updated_at: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=lambda: to_text(utc_now()),
        onupdate=lambda: to_text(utc_now()),
    )
