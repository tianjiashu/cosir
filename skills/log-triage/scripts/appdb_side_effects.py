#!/usr/bin/env python3
"""业务库副作用事实查询（log-triage skill 内置）。

单一职责：只读查询 Agent 执行留下的「磁盘与外部资源副作用」事实行：``file_snapshots``
（文件变更反向快照与保留/回退态）、``delegations``（子 Agent 委派）、
``terminal_sessions``（终端会话元数据）、``attachment_assets``（附件资产）。

职责边界：
- 负责：按 task / run 过滤并返回原始行字典。
- 不负责：会话消息（见 ``appdb_context``）、run 生命周期（见 ``appdb_agent_facts``）、
  渲染。
- 数据裁剪：``file_snapshots.op_json`` 是完整 V4A 反向操作载荷，体量大且排查不需要，
  一律不返回（只返回 ``op_chars`` 体量提示）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from appdb_readonly import query_rows, require_tables

_TERMINAL_STATUSES = ("starting", "running", "exited", "interrupted", "failed", "closed")


def list_file_snapshots(
    connection: sqlite3.Connection,
    *,
    limit: int,
    task_id: int | None = None,
    run_id: int | None = None,
) -> list[dict[str, Any]]:
    """查询文件变更快照。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        task_id: 可选任务过滤。
        run_id: 可选运行过滤（单轮回放）。

    返回:
        快照行列表（按 task_id、seq 升序），含 ``path`` / ``action`` / ``additions`` /
        ``deletions`` / ``stable`` / ``status`` / ``reverted_at`` / ``op_chars``；
        不含 ``op_json`` 原文。

    异常:
        ValueError: ``file_snapshots`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "file_snapshots")
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if run_id is not None:
        where.append("run_id = ?")
        params.append(run_id)
    sql = (
        "SELECT id, task_id, run_id, tool_call_id, tool_name, path, action, seq, "
        "additions, deletions, stable, status, reverted_at, "
        "length(op_json) AS op_chars, created_at, updated_at FROM file_snapshots"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY task_id ASC, seq ASC LIMIT ?"
    params.append(limit)
    return query_rows(connection, sql, tuple(params))


def list_delegations(
    connection: sqlite3.Connection,
    *,
    limit: int,
    task_id: int | None = None,
) -> list[dict[str, Any]]:
    """查询子 Agent 委派记录。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        task_id: 可选过滤（匹配委派发起任务或子任务）。

    返回:
        委派行列表（id 降序），含父子 run/task/agent、状态、提示与摘要。

    异常:
        ValueError: ``delegations`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "delegations")
    where = ""
    params: list[Any] = []
    if task_id is not None:
        where = " WHERE task_id = ? OR child_task_id = ?"
        params.extend([task_id, task_id])
    params.append(limit)
    sql = (
        "SELECT id, task_id, parent_run_id, child_run_id, child_task_id, parent_agent_id, "  # noqa: S608
        "child_agent_id, status, prompt, summary, error, effective_tools, created_at, updated_at "
        f"FROM delegations{where} ORDER BY id DESC LIMIT ?"
    )
    return query_rows(connection, sql, tuple(params))


def list_terminal_sessions(
    connection: sqlite3.Connection,
    *,
    limit: int,
    task_id: int | None = None,
    status: str = "",
) -> list[dict[str, Any]]:
    """查询终端会话元数据。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        task_id: 可选任务过滤。
        status: 可选会话状态精确过滤。

    返回:
        会话行列表（id 降序）。PTY 与输出缓存不落库，本表只有身份与生命周期。

    异常:
        ValueError: ``terminal_sessions`` 表缺失，或 ``status`` 非法。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "terminal_sessions")
    if status and status not in _TERMINAL_STATUSES:
        raise ValueError(f"status must be one of {', '.join(_TERMINAL_STATUSES)}")
    where: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if status:
        where.append("status = ?")
        params.append(status)
    sql = (
        "SELECT id, session_id, task_id, workspace_id, created_by_run_id, initial_cwd, "
        "shell_kind, shell_executable, worker_instance_id, worker_pid, status, end_reason, "
        "exit_code, cols, rows, last_activity_at, ended_at, created_at FROM terminal_sessions"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return query_rows(connection, sql, tuple(params))


def list_attachment_assets(
    connection: sqlite3.Connection,
    *,
    limit: int,
    task_id: int | None = None,
) -> list[dict[str, Any]]:
    """查询附件资产元数据。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        task_id: 可选任务过滤。

    返回:
        附件行列表（id 降序），含资产 id、类型、内容指纹、幂等键与存储状态。

    异常:
        ValueError: ``attachment_assets`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "attachment_assets")
    where = ""
    params: list[Any] = []
    if task_id is not None:
        where = " WHERE task_id = ?"
        params.append(task_id)
    params.append(limit)
    sql = (
        "SELECT id, task_id, asset_id, kind, content_sha256, idempotency_key, name, "  # noqa: S608
        "content_type, byte_size, width, height, storage_state, created_at, updated_at "
        f"FROM attachment_assets{where} ORDER BY id DESC LIMIT ?"
    )
    return query_rows(connection, sql, tuple(params))
