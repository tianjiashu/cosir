#!/usr/bin/env python3
"""业务库排障快照聚合（log-triage skill 内置）。

单一职责：把「一个会话」或「一次运行」在业务库中的全部相关事实聚合成单个快照对象，
让排查者用一条命令拿到该实体的完整上下文（会话 / 运行 / 消息 / 工具调用 / 子会话 / 排队消息）。

职责边界：
- 负责：编排既有查询模块并组织聚合结构。
- 不负责：单表查询实现（复用 ``appdb_agent_facts`` / ``appdb_context``）、输出渲染。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from appdb_agent_facts import list_child_sessions, recent_runs
from appdb_context import context_statistics, list_messages, summarize_tool_calls
from appdb_readonly import get_one, query_rows, require_tables

# 不取 ``tool_surface``：每个 Session 固化的整份工具 schema 很长，排障用不上。
_SESSION_COLUMNS = (
    "id, workspace_id, title, session_type, parent_session_id, derivation_depth, current_run_id, "
    "extra, created_at, updated_at"
)


def session_snapshot(connection: sqlite3.Connection, *, session_id: int, limit: int) -> dict[str, Any]:
    """汇总单个会话的排障快照。

    参数:
        connection: 只读 SQLite 连接。
        session_id: 会话标识。
        limit: 每类明细最大返回行数。

    返回:
        ``{"session", "workspace", "runs", "child_sessions", "queued_messages", "context"}``；
        ``child_sessions`` 是该会话委派出去的子会话，``queued_messages`` 是外部队列里还没并入上下文的
        消息（只列种类、投递方式与入队时间，不带正文）。

    异常:
        ValueError: 会话不存在，或必需表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "sessions", "workspaces", "runs", "session_messages")
    session = get_one(
        connection,
        f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ?",  # noqa: S608 - 列名为常量
        (session_id,),
    )
    if session is None:
        raise ValueError(f"session not found: {session_id}")
    workspace = get_one(
        connection,
        "SELECT id, name, root_path FROM workspaces WHERE id = ?",
        (session.get("workspace_id"),),
    )
    return {
        "session": session,
        "workspace": workspace,
        "runs": recent_runs(connection, limit=limit, session_id=session_id),
        "child_sessions": list_child_sessions(connection, limit=limit, parent_session_id=session_id),
        "queued_messages": query_rows(
            connection,
            "SELECT id, kind, delivery, project_to_transport, created_at FROM session_messages "
            "WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ),
        "context": context_statistics(connection, session_id=session_id),
    }


def run_snapshot(connection: sqlite3.Connection, *, run_id: int, limit: int) -> dict[str, Any]:
    """汇总单次运行的排障快照。

    参数:
        connection: 只读 SQLite 连接。
        run_id: 运行标识。
        limit: 每类明细最大返回行数。

    返回:
        ``{"run", "session", "workspace", "messages", "tool_calls"}``；
        ``messages`` 按 sequence 升序回放该 run 的消息；``run.model_config_id`` 即
        「连接 id/模型名」（模型配置在系统级 ``config/models.json``，不在库里）。

    异常:
        ValueError: 运行不存在，或必需表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "runs", "sessions", "workspaces")
    run = get_one(connection, "SELECT * FROM runs WHERE id = ?", (run_id,))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    session_id = int(run.get("session_id") or 0)
    session = get_one(
        connection,
        f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ?",  # noqa: S608 - 列名为常量
        (session_id,),
    )
    workspace = get_one(
        connection,
        "SELECT id, name, root_path FROM workspaces WHERE id = ?",
        ((session or {}).get("workspace_id"),),
    )
    return {
        "run": run,
        "session": session,
        "workspace": workspace,
        "messages": list_messages(connection, session_id=session_id, run_id=run_id, limit=limit),
        "tool_calls": summarize_tool_calls(connection, session_id=session_id, run_id=run_id, limit=limit),
    }
