#!/usr/bin/env python3
"""业务库 Agent 执行事实查询（log-triage skill 内置）。

单一职责：只读查询承载「任务 / 运行 / 工作区」的持久化事实行：``workspaces``、``tasks``、
``conversation_runs``。

职责边界：
- 负责：按标识与过滤条件下发只读 SELECT，并返回原始行字典（不做文本渲染、不做截断）。
- 不负责：会话消息与工具调用（见 ``appdb_context``）、聚合排障快照（见 ``appdb_snapshots``）、
  未收敛体检（见 ``appdb_health``）、结构概览（见 ``appdb_schema``）。

后端事实（脚本必须随 ``apps/backend/app/storage/model`` 同步）：

- 模型配置不在库里，事实源是系统级 ``.cosir/config/models.json``（含明文 Key，本 skill 不读）；
  Run 只存 ``model_config_id``，形如「连接 id/模型名」，模型名直接从它读出，没有可 JOIN 的表。
- 委派事实只由 ``tasks.parent_task_id`` / ``task_type`` 承载（见 :func:`list_child_tasks`）；
  Agent fork 的来源记在 ``tasks.extra.fork``，不写 ``parent_task_id``。
- 上下文窗口与用量属于 Run（``conversation_runs.context_window_total`` / ``usage_json``），
  Task 上不重复保存。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from appdb_readonly import contains_pattern, query_rows, require_tables

# 与后端 ``ConversationRunStatus`` 一致：Run 直接以 running 创建，没有 pending。
RUN_STATUSES = ("running", "suspended", "completed", "failed", "cancelled")
ACTIVE_RUN_STATUSES = ("running", "suspended")
TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")

_TASK_COLUMNS = (
    "t.id",
    "t.workspace_id",
    "t.title",
    "t.task_type",
    "t.parent_task_id",
    "t.derivation_depth",
    "t.current_run_id",
    "t.created_at",
    "t.updated_at",
)

_RUN_COLUMNS = (
    "r.id",
    "r.task_id",
    "r.status",
    "r.agent_id",
    "r.model_config_id",
    "r.end_reason",
    "r.final_output",
    "r.context_window_total",
    "r.usage_json",
    "r.error_json",
    "r.created_at",
    "r.updated_at",
)


def recent_workspaces(connection: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    """查询最近更新的工作区。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。

    返回:
        工作区行列表（id 降序）。

    异常:
        ValueError: ``workspaces`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "workspaces")
    return query_rows(
        connection,
        "SELECT id, name, root_path, created_at, updated_at FROM workspaces "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def recent_tasks(
    connection: sqlite3.Connection,
    *,
    limit: int,
    workspace_id: int | None = None,
    contains: str = "",
) -> list[dict[str, Any]]:
    """查询最近更新的任务，并附带当前运行的状态与模型。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        workspace_id: 可选工作区过滤。
        contains: 可选标题 / 标识模糊搜索。

    返回:
        任务行列表，每行额外含 ``current_run_status``、``current_model_config_id`` 与
        ``current_run_usage_json``（``current_run_id`` 为空或指向缺失行时为 None）。

    异常:
        ValueError: ``tasks`` / ``conversation_runs`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "tasks", "conversation_runs")
    where: list[str] = []
    params: list[Any] = []
    if workspace_id is not None:
        where.append("t.workspace_id = ?")
        params.append(workspace_id)
    if contains:
        where.append("(t.title LIKE ? ESCAPE '\\' OR CAST(t.id AS TEXT) LIKE ? ESCAPE '\\')")
        pattern = contains_pattern(contains)
        params.extend([pattern, pattern])
    # 列名来自本模块常量，过滤值全部走占位符，无注入面。
    sql = (
        f"SELECT {', '.join(_TASK_COLUMNS)}, "  # noqa: S608 - 列名来自本模块常量，值走占位符
        "r.status AS current_run_status, r.model_config_id AS current_model_config_id, "
        "r.usage_json AS current_run_usage_json "
        "FROM tasks t "
        "LEFT JOIN conversation_runs r ON r.id = t.current_run_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.updated_at DESC, t.id DESC LIMIT ?"
    params.append(limit)
    return query_rows(connection, sql, tuple(params))


def recent_runs(
    connection: sqlite3.Connection,
    *,
    limit: int,
    task_id: int | None = None,
    status: str = "",
    contains: str = "",
) -> list[dict[str, Any]]:
    """查询最近的运行（一次 Agent 执行）。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        task_id: 可选任务过滤。
        status: 可选运行状态精确过滤（见 :data:`RUN_STATUSES`）。
        contains: 可选终态原因 / 最终回复 / 错误 / 模型 id 模糊搜索。

    返回:
        运行行列表（id 降序）；``model_config_id`` 即「连接 id/模型名」。

    异常:
        ValueError: ``conversation_runs`` 表缺失，或 ``status`` 不在允许取值内。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "conversation_runs")
    if status and status not in RUN_STATUSES:
        raise ValueError(f"status must be one of {', '.join(RUN_STATUSES)}")
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("r.task_id = ?")
        params.append(task_id)
    if status:
        where.append("r.status = ?")
        params.append(status)
    if contains:
        pattern = contains_pattern(contains)
        where.append(
            "(r.model_config_id LIKE ? ESCAPE '\\' OR r.end_reason LIKE ? ESCAPE '\\' "
            "OR r.final_output LIKE ? ESCAPE '\\' OR r.error_json LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern, pattern])
    sql = (
        f"SELECT {', '.join(_RUN_COLUMNS)} FROM conversation_runs r"  # noqa: S608
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.id DESC LIMIT ?"
    params.append(limit)
    return query_rows(connection, sql, tuple(params))


def list_child_tasks(
    connection: sqlite3.Connection,
    *,
    limit: int,
    parent_task_id: int | None = None,
) -> list[dict[str, Any]]:
    """查询委派出来的子任务（``tasks`` 的直接投影）。

    语义前提（后端事实，变更时需同步）：以 ``parent_task_id IS NOT NULL`` 判定子任务。当前只有
    委派子任务（``task_type='agent_task'``）写这一列；Agent fork 的来源记在 ``extra.fork``、不写
    ``parent_task_id``，因此不在本视图中。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        parent_task_id: 可选父任务过滤；省略时返回所有子任务。

    返回:
        子任务行列表（id 降序），含父任务、派生深度与任务类型。

    异常:
        ValueError: ``tasks`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "tasks")
    where = ["parent_task_id IS NOT NULL"]
    params: list[Any] = []
    if parent_task_id is not None:
        where.append("parent_task_id = ?")
        params.append(parent_task_id)
    params.append(limit)
    sql = (
        "SELECT id, title, task_type, parent_task_id, derivation_depth, current_run_id, "  # noqa: S608
        "created_at, updated_at "
        f"FROM tasks WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
    )
    return query_rows(connection, sql, tuple(params))
