#!/usr/bin/env python3
"""业务库结构概览查询（log-triage skill 内置）。

单一职责：在不依赖任何硬编码表清单的前提下，输出在线库的真实表清单、行数与列定义，
用于排查前先确认「查的是哪个库、schema 是否已漂移」。

职责边界：
- 负责：从 ``sqlite_master`` 读取结构事实并汇总。
- 不负责：任何业务行查询（见 ``appdb_agent_facts`` / ``appdb_context`` 等）、输出渲染。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from appdb_readonly import count_rows, list_tables, table_columns


def database_overview(
    connection: sqlite3.Connection, *, include_columns: bool = True
) -> dict[str, Any]:
    """汇总当前库的表清单、行数与（可选）列定义。

    参数:
        connection: 只读 SQLite 连接。
        include_columns: 是否附带每张表的列定义；关闭可显著缩短输出。

    返回:
        ``{"tables": [{"table", "count", "columns"?}, ...]}``，表按名称升序。

    异常:
        sqlite3.Error: 读取 schema 或行数失败。

    副作用:
        无。
    """

    tables: list[dict[str, Any]] = []
    for table_name in list_tables(connection):
        entry: dict[str, Any] = {
            "table": table_name,
            "count": count_rows(connection, table_name),
        }
        if include_columns:
            entry["columns"] = table_columns(connection, table_name)
        tables.append(entry)
    return {"tables": tables}


def table_detail(connection: sqlite3.Connection, table_name: str) -> dict[str, Any]:
    """读取单张表的列定义与行数。

    参数:
        connection: 只读 SQLite 连接。
        table_name: 表名。

    返回:
        ``{"table", "count", "columns"}``。

    异常:
        ValueError: 表不存在；错误信息附上库内实际表名。
        sqlite3.Error: 读取失败。

    副作用:
        无。
    """

    tables = list_tables(connection)
    if table_name not in tables:
        raise ValueError(
            f"table not found: {table_name}; tables in this database: {', '.join(tables)}"
        )
    return {
        "table": table_name,
        "count": count_rows(connection, table_name),
        "columns": table_columns(connection, table_name),
    }
