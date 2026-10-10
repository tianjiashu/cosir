#!/usr/bin/env python3
"""业务库 Agent 执行事实查询（log-triage skill 内置）。

单一职责：只读查询承载「任务 / 运行 / Agent Team 运行 / 工作区 / 模型连接配置」的持久化事实行：
``workspaces``、``tasks``、``conversation_runs``、``agent_team_runs``、``model_configs``。

职责边界：
- 负责：按标识与过滤条件下发只读 SELECT，并返回原始行字典（不做文本渲染、不做截断）。
- 不负责：会话消息与工具调用（见 ``appdb_context``）、聚合排障快照（见 ``appdb_snapshots``）、
  终态体检（见 ``appdb_health``）、结构概览（见 ``appdb_schema``）。
- 安全边界：``model_configs.api_key`` 是明文 secret，任何查询都不得返回其原文，只返回是否存在。

数据库演进备忘（脚本必须随 ``apps/backend/app/storage/model`` 同步）：

- ``providers`` / ``models`` 两表已合并为单表 ``model_configs``；Run 只保存
  ``model_config_id``，模型名称、上下文窗口与能力不再复制进 Run，需要时 JOIN 取回。
- ``delegations`` / ``terminal_sessions`` 两表已移除：委派事实由 ``tasks.parent_task_id`` /
  ``parent_run_id`` / ``task_type`` 承载（见 :func:`list_child_tasks`），终端会话元数据不再落库。
- ``conversation_commands`` 表已移除：Transport 命令的幂等占用事实不再落库（``commandId``
  只在单次请求内做结构化去重，不入库）。同批新增 ``agent_team_runs`` 表承载 Agent Team
  运行事实，故「命令」查询位由 :func:`recent_agent_team_runs` 取代。
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
    "t.context_window_total",
    "t.created_at",
    "t.updated_at",
)

# Run 只保存 model_config_id；模型名称由 model_configs JOIN 派生，不复制在 Run 行内。
_RUN_COLUMNS = (
    "r.id",
    "r.task_id",
    "r.status",
    "r.agent_id",
    "r.model_config_id",
    "r.end_reason",
    "r.input_text",
    "r.final_output",
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
        任务行列表，每行额外含 ``current_run_status``、``current_model_config_id``、
        ``current_run_usage_json`` 与 ``current_model_name``（``current_run_id`` 为空、
        指向缺失行或 Run 未绑定配置时对应值为 None）。Task usage 不在 tasks 表重复保存。

    异常:
        ValueError: ``tasks`` / ``conversation_runs`` / ``model_configs`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "tasks", "conversation_runs", "model_configs")
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
        "r.usage_json AS current_run_usage_json, "
        "mc.model_name AS current_model_name "
        "FROM tasks t "
        "LEFT JOIN conversation_runs r ON r.id = t.current_run_id "
        "LEFT JOIN model_configs mc ON mc.id = r.model_config_id"
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
        contains: 可选输入文本 / 终态原因 / 最终回复 / 错误模糊搜索。

    返回:
        运行行列表（id 降序）；每行含 ``model_config_id``，并额外派生 ``model_name``
        （配置已删除或被置空时为 None）。

    异常:
        ValueError: ``conversation_runs`` / ``model_configs`` 表缺失，或 ``status`` 不在允许取值内。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "conversation_runs", "model_configs")
    if status and status not in _TASK_RUN_STATUSES:
        raise ValueError(f"status must be one of {', '.join(_TASK_RUN_STATUSES)}")
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
            "(r.input_text LIKE ? ESCAPE '\\' OR r.end_reason LIKE ? ESCAPE '\\' "
            "OR r.final_output LIKE ? ESCAPE '\\' OR r.error_json LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern, pattern])
    sql = (
        f"SELECT {', '.join(_RUN_COLUMNS)}, mc.model_name AS model_name "  # noqa: S608
        "FROM conversation_runs r LEFT JOIN model_configs mc ON mc.id = r.model_config_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.id DESC LIMIT ?"
    params.append(limit)
    return query_rows(connection, sql, tuple(params))


def recent_agent_team_runs(
    connection: sqlite3.Connection,
    *,
    limit: int,
    parent_task_id: int | None = None,
    parent_run_id: int | None = None,
) -> list[dict[str, Any]]:
    """查询最近的 Agent Team 运行（一次 Team 执行意图及其生命周期）。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        parent_task_id: 可选主 Agent 任务过滤。
        parent_run_id: 可选主 Agent 运行过滤（``pending`` 唯一约束的定位键之一）。

    返回:
        TeamRun 行列表（id 降序），只含标量列与生命周期字段；``state_json`` /
        ``configuration_snapshot_json`` 体积大且属内部运行态，不在此查询返回。

    异常:
        ValueError: ``agent_team_runs`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "agent_team_runs")
    where: list[str] = []
    params: list[Any] = []
    if parent_task_id is not None:
        where.append("parent_task_id = ?")
        params.append(parent_task_id)
    if parent_run_id is not None:
        where.append("parent_run_id = ?")
        params.append(parent_run_id)
    # SQL 为字面量拼接（无 f-string），过滤值全部走占位符，无注入面。
    sql = (
        "SELECT id, team_id, workspace_id, parent_task_id, parent_run_id, status, "
        "end_reason, started_at, ended_at, created_at, updated_at "
        "FROM agent_team_runs"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return query_rows(connection, sql, tuple(params))


def list_child_tasks(
    connection: sqlite3.Connection,
    *,
    limit: int,
    parent_task_id: int | None = None,
) -> list[dict[str, Any]]:
    """查询委派出来的子任务（原 ``delegations`` 表的只读替代视图）。

    委派事实现在只由 ``tasks`` 的 ``parent_task_id`` / ``parent_run_id`` / ``task_type``
    承载，因此本函数是它的直接投影，不再有独立的委派行（提示词、摘要等不再落库）。

    语义前提（后端事实，变更时需同步）：本函数以 ``parent_task_id IS NOT NULL`` 判定子任务。
    当前只有委派子任务写这两列（``task_type='delegate_task'``）；``fork`` 任务虽然也是一种
    「派生任务」，但创建时不写 ``parent_task_id``（来源记在 ``extra.fork``），因此不会出现在
    本视图中。若后端将来新增「非委派但写父引用」的任务类型，这里需要改为显式判定
    ``task_type``，否则会静默多出/漏掉行。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。
        parent_task_id: 可选父任务过滤；省略时返回所有子任务。

    返回:
        子任务行列表（id 降序），含父子 task/run 关系与任务类型。

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
        "SELECT id, title, task_type, parent_task_id, parent_run_id, current_run_id, "  # noqa: S608
        "context_window_total, created_at, updated_at "
        f"FROM tasks WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?"
    )
    return query_rows(connection, sql, tuple(params))


def list_model_configs(connection: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    """查询模型连接配置（不返回明文 Key）。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回行数。

    返回:
        配置行列表（sort_order 升序），含 ``has_api_key`` 布尔标记；**不含** ``api_key`` 原文。

    异常:
        ValueError: ``model_configs`` 表缺失。
        sqlite3.Error: 查询失败。

    副作用:
        无。
    """

    require_tables(connection, "model_configs")
    return query_rows(
        connection,
        "SELECT id, config_name, base_url, model_name, context_window_k, supports_thinking, "
        "supports_reasoning_effort, supports_image, enabled, sort_order, "
        "(api_key IS NOT NULL AND api_key != '') AS has_api_key "
        "FROM model_configs ORDER BY sort_order ASC, id ASC LIMIT ?",
        (limit,),
    )
