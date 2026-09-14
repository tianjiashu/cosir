#!/usr/bin/env python3
"""业务库排障快照聚合（log-triage skill 内置）。

单一职责：把「一个任务」或「一次运行」在业务库中的全部相关事实聚合成单个快照对象，
让排查者用一条命令拿到该实体的完整上下文（任务 / 运行 / 命令 / 消息 / 工具调用 /
文件变更 / 委派 / 子任务）。

职责边界：
- 负责：编排既有查询模块并组织聚合结构。
- 不负责：单表查询实现（复用 ``appdb_agent_facts`` / ``appdb_context`` /
  ``appdb_side_effects``）、输出渲染。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from appdb_agent_facts import recent_commands, recent_runs
from appdb_context import context_statistics, list_messages, summarize_tool_calls
from appdb_readonly import get_one, query_rows, require_tables
from appdb_side_effects import list_delegations, list_file_snapshots


def task_snapshot(connection: sqlite3.Connection, *, task_id: int, limit: int) -> dict[str, Any]:
    """汇总单个任务的排障快照。

    参数:
        connection: 只读 SQLite 连接。
        task_id: 任务标识。
        limit: 每类明细最大返回行数。

    返回:
        ``{"task", "workspace", "runs", "commands", "child_tasks", "delegations",
        "file_changes", "context"}``。

    异常:
        ValueError: 任务不存在，或必需表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "tasks", "workspaces", "conversation_runs")
    task = get_one(connection, "SELECT * FROM tasks WHERE id = ?", (task_id,))
    if task is None:
        raise ValueError(f"task not found: {task_id}")
    workspace = get_one(
        connection,
        "SELECT id, name, root_path FROM workspaces WHERE id = ?",
        (task.get("workspace_id"),),
    )
    return {
        "task": task,
        "workspace": workspace,
        "runs": recent_runs(connection, limit=limit, task_id=task_id),
        "commands": recent_commands(connection, limit=limit, task_id=task_id),
        "child_tasks": query_rows(
            connection,
            "SELECT id, title, task_type, current_run_id, delegation_id, created_at "
            "FROM tasks WHERE parent_task_id = ? ORDER BY id ASC LIMIT ?",
            (task_id, limit),
        ),
        "delegations": list_delegations(connection, limit=limit, task_id=task_id),
        "file_changes": list_file_snapshots(connection, limit=limit, task_id=task_id),
        "context": context_statistics(connection, task_id=task_id),
    }


def run_snapshot(connection: sqlite3.Connection, *, run_id: int, limit: int) -> dict[str, Any]:
    """汇总单次运行的排障快照。

    参数:
        connection: 只读 SQLite 连接。
        run_id: 运行标识。
        limit: 每类明细最大返回行数。

    返回:
        ``{"run", "task", "workspace", "commands", "messages", "tool_calls",
        "file_changes", "delegations"}``；``messages`` 按 sequence 升序回放该 run 的消息。

    异常:
        ValueError: 运行不存在，或必需表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "conversation_runs", "tasks", "workspaces")
    run = get_one(connection, "SELECT * FROM conversation_runs WHERE id = ?", (run_id,))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    task_id = int(run.get("task_id") or 0)
    task = get_one(connection, "SELECT * FROM tasks WHERE id = ?", (task_id,))
    workspace = get_one(
        connection,
        "SELECT id, name, root_path FROM workspaces WHERE id = ?",
        ((task or {}).get("workspace_id"),),
    )
    return {
        "run": run,
        "task": task,
        "workspace": workspace,
        "commands": recent_commands(connection, limit=limit, run_id=run_id),
        "messages": list_messages(connection, task_id=task_id, run_id=run_id, limit=limit),
        "tool_calls": summarize_tool_calls(connection, task_id=task_id, run_id=run_id, limit=limit),
        "file_changes": list_file_snapshots(connection, limit=limit, run_id=run_id),
        "delegations": list_delegations(connection, limit=limit, task_id=task_id),
    }
