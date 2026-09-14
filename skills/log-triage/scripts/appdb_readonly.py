#!/usr/bin/env python3
"""业务库只读访问原语（log-triage skill 内置）。

单一职责：为 ``query_app_db.py`` 及其查询模块提供「不启动服务、不导入 ``app.*``」的
SQLite 只读访问基础能力——库路径推导、只读连接、表/列探测、LIKE 转义、JSON 宽容解析
与行映射。

职责边界：
- 负责：连接与 schema 探测、SQL 执行与行转换、字面量转义、JSON 宽松解析。
- 不负责：任何业务语义查询（见 ``appdb_*`` 各查询模块）、任何输出渲染（见 CLI 入口）、
  任何写操作（连接固定 ``mode=ro``，本模块不提供写路径）。

设计边界：本模块不导入 ``app.*``，避免触发后端配置、storage 初始化或三方运行时副作用；
所有表结构事实以在线库的 ``sqlite_master`` 为准，不硬编码表名清单。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

_APP_DB_RELATIVE = ("storage", "app.sqlite3")


def repository_root() -> Path:
    """向上查找真正的仓库根目录。

    查找顺序：先按**脚本所在目录**向上找，再按**当前工作目录**向上找。后者用于 skill 被
    安装到用户级目录（如 ``~/.codebuddy/skills/``）后仍从仓库根调用的场景——此时仅靠脚本
    路径永远找不到仓库。

    参数:
        无。

    返回:
        包含 ``apps/backend`` 的仓库根绝对路径。

    异常:
        ValueError: 两条路径向上都找不到仓库根；调用方应改用 ``--db`` 显式指定数据库路径。

    副作用:
        解析当前脚本路径与当前工作目录。
    """

    for start in (Path(__file__).resolve().parent, Path.cwd()):
        for candidate in (start, *start.parents):
            if (candidate / "apps" / "backend").exists():
                return candidate
    raise ValueError(
        "cannot locate repository root (no 'apps/backend' found from the script path or the "
        "current working directory); pass --db <path> to point at the database explicitly"
    )


def default_db_path() -> Path:
    """推导默认业务数据库路径。

    参数:
        无。

    返回:
        仓库根目录下 ``storage/app.sqlite3`` 的路径。

    异常:
        无。

    副作用:
        解析当前脚本路径。
    """

    return repository_root().joinpath(*_APP_DB_RELATIVE)


def resolve_db_path(raw: str) -> Path:
    """解析 ``--db`` 参数。

    参数:
        raw: 命令行传入的数据库路径；空字符串表示使用默认路径。

    返回:
        展开用户目录后的数据库路径。

    异常:
        无。

    副作用:
        无。
    """

    return Path(raw).expanduser() if raw.strip() else default_db_path()


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读模式打开业务库连接。

    参数:
        db_path: 业务数据库路径。

    返回:
        ``sqlite3.Connection``（``row_factory`` 为 ``sqlite3.Row``）。

    异常:
        FileNotFoundError: 数据库文件不存在。
        sqlite3.OperationalError: 无法以只读模式打开。

    副作用:
        打开一个 SQLite 连接；调用方必须自行关闭。
    """

    if not db_path.exists():
        raise FileNotFoundError(f"app database not found: {db_path}")
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def list_tables(connection: sqlite3.Connection) -> list[str]:
    """列出库中全部业务表名（按名称升序）。

    参数:
        connection: 只读 SQLite 连接。

    返回:
        表名列表，不含 SQLite 内部表。

    异常:
        sqlite3.Error: 读取 schema 失败。

    副作用:
        无。
    """

    return [
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    """判断表是否存在。

    参数:
        connection: 只读 SQLite 连接。
        table_name: 表名。

    返回:
        表存在返回 True，否则 False。

    异常:
        sqlite3.Error: 读取 schema 失败。

    副作用:
        无。
    """

    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def require_tables(connection: sqlite3.Connection, *table_names: str) -> None:
    """要求给定表全部存在，否则抛出带库内表清单的错误。

    参数:
        connection: 只读 SQLite 连接。
        *table_names: 业务必需的表名。

    返回:
        无。

    异常:
        ValueError: 任一表缺失；错误信息附上库内实际表名，便于识别 schema 漂移。

    副作用:
        无。
    """

    missing = [name for name in table_names if not table_exists(connection, name)]
    if missing:
        raise ValueError(
            f"table not found: {', '.join(missing)}; "
            f"tables in this database: {', '.join(list_tables(connection))}"
        )


def table_columns(connection: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    """读取表的列定义。

    参数:
        connection: 只读 SQLite 连接。
        table_name: 表名。

    返回:
        每列 ``{"name", "type", "notnull"}`` 的列表，顺序与表定义一致。

    异常:
        sqlite3.Error: 读取失败。

    副作用:
        无。
    """

    return [
        {
            "name": row["name"],
            "type": row["type"],
            "notnull": bool(row["notnull"]),
        }
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    ]


def count_rows(connection: sqlite3.Connection, table_name: str) -> int:
    """统计表行数。

    参数:
        connection: 只读 SQLite 连接。
        table_name: 表名。

    返回:
        行数。

    异常:
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    # 表名来自库内 sqlite_master 探测结果，不含外部输入，无注入面。
    return int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])  # noqa: S608


def escape_like(value: str) -> str:
    """转义 SQLite LIKE 通配符。

    参数:
        value: 用户输入的字面搜索文本。

    返回:
        可用于 ``LIKE ? ESCAPE '\\'`` 的转义文本。

    异常:
        无。

    副作用:
        无。
    """

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def contains_pattern(value: str) -> str:
    """把字面搜索文本转换为 ``%...%`` 模糊匹配参数。

    参数:
        value: 用户输入的字面搜索文本。

    返回:
        已转义并两侧加通配符的 LIKE 参数。

    异常:
        无。

    副作用:
        无。
    """

    return f"%{escape_like(value)}%"


def safe_json(raw: str | None, *, default: Any) -> Any:
    """宽容解析 JSON 文本。

    参数:
        raw: 原始 JSON 字符串，允许为 None。
        default: 输入为空或解析失败时的返回值。

    返回:
        解析结果或 ``default``。

    异常:
        无。

    副作用:
        无。
    """

    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """把 SQLite 行转换为普通字典。

    参数:
        row: 查询得到的行。

    返回:
        列名到值的普通字典。

    异常:
        无。

    副作用:
        无。
    """

    # sqlite3.Row 的迭代产出的是「值」而非「列名」，必须显式取 keys()。
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


def query_rows(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    """执行只读查询并返回字典列表。

    参数:
        connection: 只读 SQLite 连接。
        sql: SELECT 语句。
        params: 占位符参数。

    返回:
        查询结果字典列表；无结果时为空列表。

    异常:
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    return [row_to_dict(row) for row in connection.execute(sql, params).fetchall()]


def get_one(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> dict[str, Any] | None:
    """查询单行记录。

    参数:
        connection: 只读 SQLite 连接。
        sql: SELECT 语句。
        params: 占位符参数。

    返回:
        匹配行字典；无匹配时返回 None。

    异常:
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    row = connection.execute(sql, params).fetchone()
    return row_to_dict(row) if row is not None else None
