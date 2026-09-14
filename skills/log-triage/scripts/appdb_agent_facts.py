#!/usr/bin/env python3
"""业务库 Agent 执行事实查询（log-triage skill 内置）。

单一职责：只读查询承载「任务 / 运行 / 命令 / 工作区 / 模型厂商」的持久化事实行：
``workspaces``、``tasks``、``conversation_runs``、``conversation_commands``、
``providers``、``models``。

职责边界：
- 负责：按标识与过滤条件下发只读 SELECT，并返回原始行字典（不做文本渲染、不做截断）。
- 不负责：会话消息与工具调用（见 ``appdb_context``）、聚合排障快照（见 ``appdb_snapshots``）、
  文件变更与终端会话（见 ``appdb_side_effects``）、终态体检（见 ``appdb_health``）。
- 安全边界：``providers.api_key`` 是明文 secret，任何查询都不得返回其原文，只返回是否存在。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from appdb_readonly import contains_pattern, query_rows, require_tables

_TASK_RUN_STATUSES = ("pending", "running", "completed", "failed", "cancelled")

_TASK_COLUMNS = (
    "t.id",
    "t.workspace_id",
    "t.title",
    "t.task_type",
    "t.parent_task_id",
    "t.parent_run_id",
    "t.current_run_id",
    "t.delegation_id",
    "t.context_usage_used",
    "t.context_window_total",
    "t.created_at",
    "t.updated_at",
)

_RUN_COLUMNS = (
    "id",
    "task_id",
    "status",
    "agent_id",
    "provider_id",
    "model_name",
    "reasoning_effort",
    "end_reason",
    "input_text",
    "final_output",
    "usage_json",
    "error_json",
    "created_at",
    "updated_at",
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
        任务行列表，每行额外含 ``current_run_status`` 与 ``current_model_name``
        （``current_run_id`` 为空或指向缺失行时为 None）。

    异常:
        ValueError: ``tasks`` 表缺失。
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
        "r.status AS current_run_status, r.model_name AS current_model_name "
        "FROM tasks t LEFT JOIN conversation_runs r ON r.id = t.current_run_id"
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
        status: 可选运行状态精确过滤（pending / running / completed / failed / cancelled）。
        contains: 可选输入文本 / 终态原因 / 最终回复模糊搜索。

    返回:
        运行行列表（id 降序）。

    异常:
        ValueError: ``conversation_runs`` 表缺失，或 ``status`` 不在允许取值内。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "conversation_runs")
    if status and status not in _TASK_RUN_STATUSES:
        raise ValueError(f"status must be one of {', '.join(_TASK_RUN_STATUSES)}")
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if status:
        where.append("status = ?")
        params.append(status)
    if contains:
        pattern = contains_pattern(contains)
        where.append(
            "(input_text LIKE ? ESCAPE '\\' OR end_reason LIKE ? ESCAPE '\\' "
            "OR final_output LIKE ? ESCAPE '\\' OR error_json LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern, pattern])
    sql = f"SELECT {', '.join(_RUN_COLUMNS)} FROM conversation_runs"  # noqa: S608
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return query_rows(connection, sql, tuple(params))


def recent_commands(
    connection: sqlite3.Connection,
    *,
    limit: int,
    task_id: int | None = None,
    run_id: int | None = None,
) -> list[dict[str, Any]]:
    """查询最近的 Assistant Transport 命令（幂等占用事实）。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        task_id: 可选任务过滤。
        run_id: 可选运行过滤。

    返回:
        命令行列表（id 降序）。

    异常:
        ValueError: ``conversation_commands`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "conversation_commands")
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if run_id is not None:
        where.append("run_id = ?")
        params.append(run_id)
    sql = (
        "SELECT id, task_id, command_id, command_type, payload_hash, run_id, "
        "error_code, created_at FROM conversation_commands"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return query_rows(connection, sql, tuple(params))


def list_providers(connection: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    """查询模型厂商配置（不返回明文 Key）。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。

    返回:
        厂商行列表，含 ``has_api_key`` 布尔标记；**不含** ``api_key`` 原文。

    异常:
        ValueError: ``providers`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "providers")
    return query_rows(
        connection,
        "SELECT id, name, type, base_url, enabled, sort_order, "
        "(api_key IS NOT NULL AND api_key != '') AS has_api_key "
        "FROM providers ORDER BY sort_order ASC, id ASC LIMIT ?",
        (limit,),
    )


def list_models(
    connection: sqlite3.Connection,
    *,
    limit: int,
    provider_id: int | None = None,
) -> list[dict[str, Any]]:
    """查询模型条目。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        provider_id: 可选厂商过滤。

    返回:
        模型行列表（含上下文窗口与能力标记）。

    异常:
        ValueError: ``models`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "models")
    where = ""
    params: list[Any] = []
    if provider_id is not None:
        where = " WHERE provider_id = ?"
        params.append(provider_id)
    params.append(limit)
    sql = (
        "SELECT id, provider_id, model_name, display_name, max_context_window, "  # noqa: S608
        "supports_thinking, supports_image, supports_video, enabled, sort_order "
        f"FROM models{where} ORDER BY sort_order ASC, id ASC LIMIT ?"
    )
    return query_rows(connection, sql, tuple(params))
