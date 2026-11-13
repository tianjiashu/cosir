#!/usr/bin/env python3
"""业务库未收敛状态体检（log-triage skill 内置）。

单一职责：找出「本该收敛但仍是活跃态」的业务行，用于排查卡死、任务一直显示运行中、消息排着没人处理、
重启后遗留脏数据等问题。

职责边界：
- 负责：活跃 run（running / suspended）、指向活跃 run 的任务、仍未结算的工具占位、外部队列里还没
  并入上下文的消息。
- 不负责：修复（本 skill 全程只读）、run 生命周期规则判定（状态取值以 ``conversation_runs.status``
  与后端 ``ConversationRunStatus`` 为准）。

判读口径：``suspended`` 是正常的等待态（等后台结果、等用户决定），不等于卡死，要结合未结算占位与
队列一起看；后端重启时遗留的 ``running`` 会被收敛为 ``cancelled``，所以重启后仍是 ``running`` 而
又没有执行中的进程，才是真卡住。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from appdb_agent_facts import ACTIVE_RUN_STATUSES
from appdb_readonly import query_rows, require_tables


def find_unsettled(connection: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    """汇总未收敛的运行、任务、工具占位与排队消息。

    参数:
        connection: 只读 SQLite 连接。
        limit: 每类最大返回行数。

    返回:
        ``{"active_runs", "tasks_with_active_run", "open_tool_placeholders", "queued_messages"}``：
        非终态 run（含任务标题与工作区根）、``current_run_id`` 指向非终态 run 的任务、metadata 状态
        仍为 ``pending`` / ``running`` 的工具结果行（带所属 run 状态与是否后台占位），以及按 Task 汇总
        的外部队列积压（条数、种类、最早入队时间）。

    异常:
        ValueError: 必需表缺失。
        sqlite3.Error: 查询失败。
    """

    require_tables(
        connection,
        "conversation_runs",
        "conversation_task_contexts",
        "task_messages",
        "tasks",
        "workspaces",
    )
    active = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)

    # 表名/列名均为内部常量，状态枚举走占位符，无注入面。
    active_runs = query_rows(
        connection,
        "SELECT r.id, r.task_id, r.status, r.agent_id, r.model_config_id, "  # noqa: S608
        "r.created_at, r.updated_at, t.title, w.root_path "
        "FROM conversation_runs r "
        "LEFT JOIN tasks t ON t.id = r.task_id "
        "LEFT JOIN workspaces w ON w.id = t.workspace_id "
        f"WHERE r.status IN ({active}) "
        "ORDER BY r.updated_at ASC, r.id ASC LIMIT ?",
        (*ACTIVE_RUN_STATUSES, limit),
    )
    tasks_with_active_run = query_rows(
        connection,
        "SELECT t.id, t.title, t.current_run_id, r.status AS current_run_status, "  # noqa: S608
        "r.updated_at AS run_updated_at "
        "FROM tasks t JOIN conversation_runs r ON r.id = t.current_run_id "
        f"WHERE r.status IN ({active}) "
        "ORDER BY t.updated_at ASC, t.id ASC LIMIT ?",
        (*ACTIVE_RUN_STATUSES, limit),
    )
    open_placeholders = query_rows(
        connection,
        "SELECT c.id, c.task_id, c.run_id, c.sequence, c.turn, "
        "json_extract(c.message_json, '$.tool_call_id') AS tool_call_id, "
        "json_extract(c.metadata_json, '$.status') AS tool_status, "
        "json_extract(c.metadata_json, '$.background') IS NOT NULL AS is_background, "
        "r.status AS run_status "
        "FROM conversation_task_contexts c LEFT JOIN conversation_runs r ON r.id = c.run_id "
        "WHERE json_extract(c.message_json, '$.role') = 'tool' "
        "AND json_extract(c.metadata_json, '$.status') IN ('pending', 'running') "
        "ORDER BY c.task_id ASC, c.sequence ASC LIMIT ?",
        (limit,),
    )
    queued = query_rows(
        connection,
        "SELECT q.task_id, t.title, COUNT(*) AS queued, "
        "GROUP_CONCAT(DISTINCT q.kind) AS kinds, MIN(q.created_at) AS oldest "
        "FROM task_messages q LEFT JOIN tasks t ON t.id = q.task_id "
        "GROUP BY q.task_id ORDER BY oldest ASC LIMIT ?",
        (limit,),
    )
    return {
        "active_runs": active_runs,
        "tasks_with_active_run": tasks_with_active_run,
        "open_tool_placeholders": open_placeholders,
        "queued_messages": queued,
    }
