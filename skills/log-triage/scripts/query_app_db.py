#!/usr/bin/env python3
"""本地业务数据库排查 CLI（log-triage skill 内置脚本）。

单一职责：以只读方式查询 ``storage/app.sqlite3`` 中由
``apps/backend/app/storage/model`` 定义的业务表，帮助 Agent 在不启动 FastAPI / Tauri /
LangGraph 的情况下排查任务、轮次、事件流、委派与文件变更问题。

脚本刻意不导入 ``app.*``，避免触发后端配置、storage 初始化或三方运行时副作用。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 10000
_ORDERS = ("asc", "desc")
_KNOWN_TABLES = (
    "workspaces",
    "tasks",
    "turns",
    "turn_messages",
    "runtime_events",
    "delegations",
    "file_snapshots",
)
_TASK_COLUMNS = (
    "task_id",
    "workspace_id",
    "agent_id",
    "title",
    "status",
    "task_type",
    "parent_task_id",
    "parent_turn_id",
    "delegation_id",
    "context_usage_used",
    "created_at",
    "updated_at",
)
_TURN_COLUMNS = (
    "turn_id",
    "task_id",
    "input_text",
    "status",
    "end_reason",
    "response_text",
    "created_at",
    "updated_at",
)
_EVENT_COLUMNS = (
    "turn_id",
    "sequence",
    "event_id",
    "event_type",
    "task_id",
    "payload_json",
    "created_at",
)


def repository_root() -> Path:
    """向上查找真正的仓库根。

    参数:
        无。
    返回:
        包含 ``apps/backend`` 的仓库根路径。
    异常:
        无。
    副作用:
        解析当前脚本路径。
    """

    current = Path(__file__).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / "apps" / "backend" / "app" / "storage" / "model").exists():
            return candidate
    return Path(__file__).resolve().parent.parent


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

    return repository_root() / "storage" / "app.sqlite3"


def resolve_db_path(raw: str) -> Path:
    """解析 ``--db`` 参数。

    参数:
        raw: 命令行传入的数据库路径；空字符串表示使用默认路径。
    返回:
        展开后的数据库路径。
    异常:
        无。
    副作用:
        无。
    """

    return Path(raw).expanduser() if raw.strip() else default_db_path()


def normalize_limit(limit: int) -> int:
    """校验查询数量上限。

    参数:
        limit: 命令行传入的 limit。
    返回:
        合法 limit。
    异常:
        ValueError: 如果 limit 不在允许范围。
    副作用:
        无。
    """

    if limit < 1:
        raise ValueError("limit must be greater than zero")
    if limit > _MAX_LIMIT:
        raise ValueError(f"limit must be less than or equal to {_MAX_LIMIT}")
    return limit


def normalize_order(order: str) -> str:
    """校验排序方向。

    参数:
        order: 命令行传入的排序方向。
    返回:
        ``asc`` 或 ``desc``。
    异常:
        ValueError: 如果排序方向非法。
    副作用:
        无。
    """

    normalized = order.strip().lower()
    if normalized not in _ORDERS:
        raise ValueError("order must be asc or desc")
    return normalized


def escape_like(value: str) -> str:
    """转义 SQLite LIKE 通配符。

    参数:
        value: 用户输入的字面搜索文本。
    返回:
        可用于 ``LIKE ... ESCAPE '\\'`` 的转义文本。
    异常:
        无。
    副作用:
        无。
    """

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读模式打开业务库连接。

    参数:
        db_path: 业务数据库路径。
    返回:
        ``sqlite3.Connection``，行工厂为 ``sqlite3.Row``。
    异常:
        FileNotFoundError: 如果数据库文件不存在。
        sqlite3.OperationalError: 如果无法以只读模式打开。
    副作用:
        打开 SQLite 连接。
    """

    if not db_path.exists():
        raise FileNotFoundError(f"app database not found: {db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """把 SQLite 行转换为普通字典。

    参数:
        row: 查询得到的行。
    返回:
        普通字典。
    异常:
        无。
    副作用:
        无。
    """

    return {key: row[key] for key in row.keys()}


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    """判断表是否存在。

    参数:
        connection: 只读 SQLite 连接。
        table_name: 表名。
    返回:
        表存在返回 True，否则 False。
    异常:
        sqlite3.Error: 如果读取 schema 失败。
    副作用:
        无。
    """

    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def require_table(connection: sqlite3.Connection, table_name: str) -> None:
    """要求指定表存在。

    参数:
        connection: 只读 SQLite 连接。
        table_name: 表名。
    返回:
        无。
    异常:
        ValueError: 如果表不存在。
        sqlite3.Error: 如果读取 schema 失败。
    副作用:
        无。
    """

    if not table_exists(connection, table_name):
        raise ValueError(f"table not found: {table_name}")


def safe_json(raw: str | None, *, default: Any) -> Any:
    """宽容解析 JSON 文本。

    参数:
        raw: 原始 JSON 字符串。
        default: 输入为空或解析失败时的返回值。
    返回:
        JSON 解析结果或 default。
    异常:
        无。
    副作用:
        无。
    """

    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def compact_text(value: Any, *, max_chars: int = 160) -> str:
    """压缩长文本，便于终端排查。

    参数:
        value: 待渲染的任意值。
        max_chars: 最大字符数。
    返回:
        单行短文本。
    异常:
        无。
    副作用:
        无。
    """

    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def summarize_payload(payload_json: str | None) -> dict[str, Any]:
    """从 runtime event payload 中提取排障摘要。

    参数:
        payload_json: ``runtime_events.payload_json`` 原文。
    返回:
        包含关键字段的摘要字典。
    异常:
        无。
    副作用:
        无。
    """

    payload = safe_json(payload_json, default={})
    if not isinstance(payload, dict):
        return {"raw": compact_text(payload_json)}

    summary: dict[str, Any] = {}
    for key in (
        "status",
        "message",
        "text",
        "reason",
        "error",
        "tool_name",
        "tool_call_id",
        "agent_id",
        "trace_id",
        "run_id",
        "step_id",
    ):
        if key in payload and payload[key] not in (None, "", [], {}):
            summary[key] = compact_text(payload[key])
    for nested_key in ("data", "metadata"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            for key in ("task_id", "turn_id", "path", "command", "exit_code", "duration_ms"):
                if key in nested and nested[key] not in (None, "", [], {}):
                    summary[f"{nested_key}.{key}"] = compact_text(nested[key])
    if not summary:
        summary["keys"] = ",".join(sorted(str(key) for key in payload.keys())[:12])
    return summary


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
        查询结果字典列表。
    异常:
        sqlite3.Error: 如果查询失败。
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
        sqlite3.Error: 如果查询失败。
    副作用:
        无。
    """

    row = connection.execute(sql, params).fetchone()
    return row_to_dict(row) if row is not None else None


def inspect_schema(connection: sqlite3.Connection) -> dict[str, Any]:
    """读取业务库表结构与行数概览。

    参数:
        connection: 只读 SQLite 连接。
    返回:
        表结构概览。
    异常:
        sqlite3.Error: 如果读取失败。
    副作用:
        无。
    """

    tables: list[dict[str, Any]] = []
    for table_name in _KNOWN_TABLES:
        if not table_exists(connection, table_name):
            tables.append({"table": table_name, "exists": False})
            continue
        count = int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
        columns = [
            {"name": row["name"], "type": row["type"], "notnull": bool(row["notnull"])}
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        ]
        tables.append({"table": table_name, "exists": True, "count": count, "columns": columns})
    return {"database": str(default_db_path()), "tables": tables}


def recent_tasks(
    connection: sqlite3.Connection,
    *,
    limit: int,
    status: str,
    contains: str,
) -> list[dict[str, Any]]:
    """查询最近更新的任务。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回数量。
        status: 可选任务状态过滤。
        contains: 可选标题 / id / agent 模糊搜索。
    返回:
        任务列表。
    异常:
        sqlite3.Error: 如果查询失败。
    副作用:
        无。
    """

    require_table(connection, "tasks")
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if contains:
        pattern = f"%{escape_like(contains)}%"
        where.append(
            "(task_id LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR agent_id LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern])
    sql = f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, task_id DESC LIMIT ?"
    params.append(limit)
    return query_rows(connection, sql, tuple(params))


def recent_turns(
    connection: sqlite3.Connection,
    *,
    limit: int,
    status: str,
    task_id: str,
    contains: str,
) -> list[dict[str, Any]]:
    """查询最近更新的轮次。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回数量。
        status: 可选 turn 状态过滤。
        task_id: 可选 task_id 过滤。
        contains: 可选输入 / 输出 / id 模糊搜索。
    返回:
        轮次列表。
    异常:
        sqlite3.Error: 如果查询失败。
    副作用:
        无。
    """

    require_table(connection, "turns")
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if task_id:
        where.append("task_id = ?")
        params.append(task_id)
    if contains:
        pattern = f"%{escape_like(contains)}%"
        where.append(
            "("
            "turn_id LIKE ? ESCAPE '\\' OR input_text LIKE ? ESCAPE '\\' "
            "OR response_text LIKE ? ESCAPE '\\' OR end_reason LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([pattern, pattern, pattern, pattern])
    sql = f"SELECT {', '.join(_TURN_COLUMNS)} FROM turns"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, turn_id DESC LIMIT ?"
    params.append(limit)
    return query_rows(connection, sql, tuple(params))


def search_events(
    connection: sqlite3.Connection,
    *,
    limit: int,
    order: str,
    task_id: str,
    turn_id: str,
    event_type: str,
    contains: str,
) -> list[dict[str, Any]]:
    """查询 runtime event 流。

    参数:
        connection: 只读 SQLite 连接。
        limit: 最大返回数量。
        order: 排序方向。
        task_id: 可选 task_id。
        turn_id: 可选 turn_id。
        event_type: 可选事件类型精确过滤。
        contains: 可选 payload / event_type / event_id 模糊搜索。
    返回:
        事件列表，附带 ``payload_summary``。
    异常:
        sqlite3.Error: 如果查询失败。
    副作用:
        无。
    """

    require_table(connection, "runtime_events")
    where: list[str] = []
    params: list[Any] = []
    for column, value in (("task_id", task_id), ("turn_id", turn_id), ("event_type", event_type)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if contains:
        pattern = f"%{escape_like(contains)}%"
        where.append(
            "("
            "event_id LIKE ? ESCAPE '\\' OR event_type LIKE ? ESCAPE '\\' "
            "OR payload_json LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([pattern, pattern, pattern])
    direction = "ASC" if order == "asc" else "DESC"
    sql = f"SELECT {', '.join(_EVENT_COLUMNS)} FROM runtime_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY created_at {direction}, sequence {direction} LIMIT ?"
    params.append(limit)
    rows = query_rows(connection, sql, tuple(params))
    for row in rows:
        row["payload_summary"] = summarize_payload(row.get("payload_json"))
    return rows


def get_task_snapshot(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    limit: int,
) -> dict[str, Any]:
    """读取单个 task 的排障快照。

    参数:
        connection: 只读 SQLite 连接。
        task_id: 任务标识。
        limit: 每类明细最大返回数量。
    返回:
        task 相关的 workspace、turn、event、delegation 聚合信息。
    异常:
        ValueError: 如果 task 不存在。
        sqlite3.Error: 如果查询失败。
    副作用:
        无。
    """

    require_table(connection, "tasks")
    task = get_one(connection, f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks WHERE task_id = ?", (task_id,))
    if task is None:
        raise ValueError(f"task not found: {task_id}")

    workspace = None
    if table_exists(connection, "workspaces"):
        workspace = get_one(
            connection,
            "SELECT workspace_id, name, root_path, created_at, updated_at FROM workspaces "
            "WHERE workspace_id = ?",
            (task["workspace_id"],),
        )
    turns = recent_turns(connection, limit=limit, status="", task_id=task_id, contains="")
    event_counts = query_rows(
        connection,
        "SELECT event_type, COUNT(*) AS count FROM runtime_events "
        "WHERE task_id = ? GROUP BY event_type ORDER BY count DESC, event_type ASC",
        (task_id,),
    ) if table_exists(connection, "runtime_events") else []
    last_events = search_events(
        connection,
        limit=limit,
        order="desc",
        task_id=task_id,
        turn_id="",
        event_type="",
        contains="",
    ) if table_exists(connection, "runtime_events") else []
    delegations = query_rows(
        connection,
        "SELECT * FROM delegations WHERE task_id = ? OR child_task_id = ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (task_id, task_id, limit),
    ) if table_exists(connection, "delegations") else []
    child_tasks = query_rows(
        connection,
        f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks WHERE parent_task_id = ? "
        "ORDER BY created_at ASC LIMIT ?",
        (task_id, limit),
    )
    return {
        "task": task,
        "workspace": workspace,
        "turns": turns,
        "child_tasks": child_tasks,
        "event_counts": event_counts,
        "last_events": last_events,
        "delegations": delegations,
    }


def get_turn_snapshot(
    connection: sqlite3.Connection,
    *,
    turn_id: str,
    limit: int,
) -> dict[str, Any]:
    """读取单个 turn 的排障快照。

    参数:
        connection: 只读 SQLite 连接。
        turn_id: 轮次标识。
        limit: 每类明细最大返回数量。
    返回:
        turn 相关的 task、messages、runtime events、文件快照与委派信息。
    异常:
        ValueError: 如果 turn 不存在。
        sqlite3.Error: 如果查询失败。
    副作用:
        无。
    """

    require_table(connection, "turns")
    turn = get_one(connection, f"SELECT {', '.join(_TURN_COLUMNS)} FROM turns WHERE turn_id = ?", (turn_id,))
    if turn is None:
        raise ValueError(f"turn not found: {turn_id}")

    task = get_one(
        connection,
        f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks WHERE task_id = ?",
        (turn["task_id"],),
    ) if table_exists(connection, "tasks") else None
    messages = query_rows(
        connection,
        "SELECT turn_id, sequence, role, content_text, metadata_json, in_context "
        "FROM turn_messages WHERE turn_id = ? ORDER BY sequence ASC LIMIT ?",
        (turn_id, limit),
    ) if table_exists(connection, "turn_messages") else []
    events = search_events(
        connection,
        limit=limit,
        order="asc",
        task_id="",
        turn_id=turn_id,
        event_type="",
        contains="",
    ) if table_exists(connection, "runtime_events") else []
    file_snapshots = query_rows(
        connection,
        "SELECT id, turn_id, tool_call_id, tool_name, path, action, seq, additions, deletions, "
        "stable, status, reverted_at FROM file_snapshots WHERE turn_id = ? ORDER BY seq ASC LIMIT ?",
        (turn_id, limit),
    ) if table_exists(connection, "file_snapshots") else []
    delegations = query_rows(
        connection,
        "SELECT * FROM delegations WHERE parent_turn_id = ? OR child_turn_id = ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (turn_id, turn_id, limit),
    ) if table_exists(connection, "delegations") else []
    return {
        "turn": turn,
        "task": task,
        "messages": messages,
        "events": events,
        "file_snapshots": file_snapshots,
        "delegations": delegations,
    }


def find_stuck_records(connection: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    """查找疑似未收敛的任务和轮次。

    参数:
        connection: 只读 SQLite 连接。
        limit: 每类最大返回数量。
    返回:
        非终态 task / turn 列表。
    异常:
        sqlite3.Error: 如果查询失败。
    副作用:
        无。
    """

    terminal_statuses = ("completed", "failed", "cancelled", "canceled")
    placeholders = ", ".join("?" for _ in terminal_statuses)
    tasks = query_rows(
        connection,
        f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks "
        f"WHERE lower(status) NOT IN ({placeholders}) "
        "ORDER BY updated_at ASC, task_id ASC LIMIT ?",
        (*terminal_statuses, limit),
    ) if table_exists(connection, "tasks") else []
    turns = query_rows(
        connection,
        f"SELECT {', '.join(_TURN_COLUMNS)} FROM turns "
        f"WHERE lower(status) NOT IN ({placeholders}) "
        "ORDER BY updated_at ASC, turn_id ASC LIMIT ?",
        (*terminal_statuses, limit),
    ) if table_exists(connection, "turns") else []
    return {"tasks": tasks, "turns": turns}


def render_text(value: Any) -> str:
    """把查询结果渲染为适合终端阅读的文本。

    参数:
        value: 查询结果。
    返回:
        多行文本。
    异常:
        无。
    副作用:
        无。
    """

    if isinstance(value, list):
        return "\n".join(render_mapping(item) for item in value)
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            lines.append(f"[{key}]")
            if isinstance(item, list):
                lines.extend(render_mapping(row) for row in item)
            elif isinstance(item, dict):
                lines.append(render_mapping(item))
            elif item is None:
                lines.append("(none)")
            else:
                lines.append(compact_text(item))
        return "\n".join(line for line in lines if line != "")
    return compact_text(value)


def render_mapping(row: dict[str, Any]) -> str:
    """渲染单个字典为一行文本。

    参数:
        row: 待渲染字典。
    返回:
        单行文本。
    异常:
        无。
    副作用:
        无。
    """

    parts: list[str] = []
    for key, value in row.items():
        if key == "payload_json":
            continue
        if key in {"input_text", "response_text", "content_text", "prompt", "summary", "error"}:
            value = compact_text(value)
        elif isinstance(value, dict | list):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"{key}={value}")
    return " ".join(parts)


def emit_result(value: Any, *, output_format: str) -> None:
    """输出查询结果。

    参数:
        value: 查询结果。
        output_format: ``text`` 或 ``json``。
    返回:
        无。
    异常:
        ValueError: 如果输出格式非法。
    副作用:
        写 stdout。
    """

    if output_format == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if output_format == "text":
        print(render_text(value))
        return
    raise ValueError("format must be text or json")


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。

    参数:
        无。
    返回:
        配置好的 ``ArgumentParser``。
    异常:
        无。
    副作用:
        无。
    """

    parser = argparse.ArgumentParser(
        prog="query_app_db",
        description="只读查询 coding-agent 本地业务数据库 storage/app.sqlite3。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    schema = subparsers.add_parser("schema", help="查看业务表是否存在、行数与列。")
    _add_common_options(schema)

    tasks = subparsers.add_parser("tasks", help="查询最近任务。")
    _add_common_options(tasks)
    tasks.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    tasks.add_argument("--status", default="", help="按任务状态过滤。")
    tasks.add_argument("--contains", default="", help="按 task_id/title/agent_id 模糊搜索。")

    turns = subparsers.add_parser("turns", help="查询最近轮次。")
    _add_common_options(turns)
    turns.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    turns.add_argument("--status", default="", help="按 turn 状态过滤。")
    turns.add_argument("--task-id", default="", help="按 task_id 过滤。")
    turns.add_argument("--contains", default="", help="按 turn_id/input/response/end_reason 搜索。")

    task = subparsers.add_parser("task", help="查看单个 task 的排障快照。")
    _add_common_options(task)
    task.add_argument("task_id")
    task.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)

    turn = subparsers.add_parser("turn", help="查看单个 turn 的排障快照。")
    _add_common_options(turn)
    turn.add_argument("turn_id")
    turn.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)

    events = subparsers.add_parser("events", help="查询 runtime_events 事件流。")
    _add_common_options(events)
    events.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    events.add_argument("--order", choices=_ORDERS, default="desc")
    events.add_argument("--task-id", default="", help="按 task_id 过滤。")
    events.add_argument("--turn-id", default="", help="按 turn_id 过滤。")
    events.add_argument("--type", dest="event_type", default="", help="按 event_type 精确过滤。")
    events.add_argument("--contains", default="", help="搜索 event_id/event_type/payload_json。")

    stuck = subparsers.add_parser("stuck", help="查找疑似未进入终态的任务和轮次。")
    _add_common_options(stuck)
    stuck.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    return parser


def _add_common_options(sub: argparse.ArgumentParser) -> None:
    """为子命令追加通用输出选项。

    参数:
        sub: 子命令解析器。
    返回:
        无。
    异常:
        无。
    副作用:
        向子命令注册 ``--db`` 与 ``--format``，使选项可放在子命令之后。
    """

    sub.add_argument("--db", default="", help="业务 SQLite 文件路径，默认 storage/app.sqlite3。")
    sub.add_argument("--format", choices=("text", "json"), default="text", help="输出格式。")


def run(args: argparse.Namespace) -> Any:
    """按命令执行查询。

    参数:
        args: argparse 解析后的命名空间。
    返回:
        查询结果。
    异常:
        ValueError: 如果参数或数据状态非法。
        sqlite3.Error: 如果查询失败。
    副作用:
        打开并关闭 SQLite 连接。
    """

    db_path = resolve_db_path(args.db)
    connection = open_readonly(db_path)
    try:
        if args.command == "schema":
            result = inspect_schema(connection)
            result["database"] = str(db_path)
            return result
        if args.command == "tasks":
            return recent_tasks(
                connection,
                limit=normalize_limit(args.limit),
                status=args.status.strip(),
                contains=args.contains.strip(),
            )
        if args.command == "turns":
            return recent_turns(
                connection,
                limit=normalize_limit(args.limit),
                status=args.status.strip(),
                task_id=args.task_id.strip(),
                contains=args.contains.strip(),
            )
        if args.command == "task":
            return get_task_snapshot(
                connection,
                task_id=args.task_id.strip(),
                limit=normalize_limit(args.limit),
            )
        if args.command == "turn":
            return get_turn_snapshot(
                connection,
                turn_id=args.turn_id.strip(),
                limit=normalize_limit(args.limit),
            )
        if args.command == "events":
            return search_events(
                connection,
                limit=normalize_limit(args.limit),
                order=normalize_order(args.order),
                task_id=args.task_id.strip(),
                turn_id=args.turn_id.strip(),
                event_type=args.event_type.strip(),
                contains=args.contains.strip(),
            )
        if args.command == "stuck":
            return find_stuck_records(connection, limit=normalize_limit(args.limit))
        raise ValueError(f"unknown command: {args.command}")
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    参数:
        argv: 命令行参数列表；省略时使用 ``sys.argv[1:]``。
    返回:
        进程退出码，0 表示成功，1 表示用户错误或查询失败。
    异常:
        无。
    副作用:
        读取 SQLite；写 stdout/stderr。
    """

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        emit_result(run(args), output_format=args.format)
    except (ValueError, FileNotFoundError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
