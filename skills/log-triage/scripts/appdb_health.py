#!/usr/bin/env python3
"""业务库未收敛状态体检（log-triage skill 内置）。

单一职责：找出「本该收敛但仍是活跃态」的业务行，用于排查卡死、任务一直显示运行中、
重启后遗留脏数据等问题。

职责边界：
- 负责：活跃 run、指向活跃 run 的任务、以及所属 run 已终态却仍残留的流式草稿。
- 不负责：修复（本 skill 全程只读）、run 生命周期规则判定（状态取值以
  ``conversation_runs.status`` 列与 ``ConversationRunStatus`` 为准）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from appdb_readonly import query_rows, require_tables
from sqlite_values import bool_true_sql

TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")
ACTIVE_RUN_STATUSES = ("pending", "running")


def find_unsettled(connection: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    """汇总未收敛的运行、任务与残留流式草稿。

    参数:
        connection: 只读 SQLite 连接。
        limit: 每类最大返回行数。

    返回:
        ``{"active_runs", "tasks_with_active_run", "stale_streaming_drafts"}``：
        非终态 run（含任务标题与工作区根）、``current_run_id`` 指向非终态 run 的任务、
        ``is_streaming=1`` 且所属 run 已终态（或已不存在）的上下文草稿行。

    异常:
        ValueError: ``conversation_runs`` 或 ``conversation_task_contexts`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(
        connection, "conversation_runs", "conversation_task_contexts", "tasks", "workspaces"
    )
    active_placeholders = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
    terminal_placeholders = ", ".join("?" for _ in TERMINAL_RUN_STATUSES)

    # 三处 SQL 的表名/列名均为内部常量，状态枚举走占位符，无注入面。
    active_runs_sql = (
        "SELECT r.id, r.task_id, r.status, r.agent_id, r.model_name, r.created_at, r.updated_at, "  # noqa: S608
        "t.title, w.root_path "
        "FROM conversation_runs r "
        "LEFT JOIN tasks t ON t.id = r.task_id "
        "LEFT JOIN workspaces w ON w.id = t.workspace_id "
        f"WHERE r.status IN ({active_placeholders}) "
        "ORDER BY r.updated_at ASC, r.id ASC LIMIT ?"
    )
    active_runs = query_rows(connection, active_runs_sql, (*ACTIVE_RUN_STATUSES, limit))
    tasks_with_active_run_sql = (
        "SELECT t.id, t.title, t.current_run_id, r.status AS current_run_status, "  # noqa: S608
        "r.updated_at AS run_updated_at "
        "FROM tasks t JOIN conversation_runs r ON r.id = t.current_run_id "
        f"WHERE r.status IN ({active_placeholders}) "
        "ORDER BY t.updated_at ASC, t.id ASC LIMIT ?"
    )
    tasks_with_active_run = query_rows(
        connection, tasks_with_active_run_sql, (*ACTIVE_RUN_STATUSES, limit)
    )
    stale_drafts_sql = (
        "SELECT c.id, c.task_id, c.run_id, c.sequence, c.is_streaming, r.status AS run_status "  # noqa: S608
        "FROM conversation_task_contexts c LEFT JOIN conversation_runs r ON r.id = c.run_id "
        f"WHERE {bool_true_sql('c.is_streaming')} "
        f"AND (r.id IS NULL OR r.status IN ({terminal_placeholders})) "
        "ORDER BY c.task_id ASC, c.sequence ASC LIMIT ?"
    )
    stale_drafts = query_rows(connection, stale_drafts_sql, (*TERMINAL_RUN_STATUSES, limit))
    return {
        "active_runs": active_runs,
        "tasks_with_active_run": tasks_with_active_run,
        "stale_streaming_drafts": stale_drafts,
    }
