#!/usr/bin/env python3
"""本地业务数据库排查 CLI（log-triage skill 内置）。

单一职责：CLI 表现层——解析参数、把子命令分发给对应查询模块、把结果渲染为文本或 JSON。
脚本以只读方式直连 ``storage/app.sqlite3``，不启动 FastAPI / Tauri / LangGraph，也不导入
``app.*``，因此可在服务未启动、启动失败或 UI 打不开时使用。

职责边界：
- 负责：argparse 参数面、子命令分发、输出渲染（文本/JSON）、退出码。
- 不负责：SQL 与业务语义（见 ``appdb_readonly`` / ``appdb_agent_facts`` /
  ``appdb_context`` / ``appdb_side_effects`` / ``appdb_snapshots`` / ``appdb_health`` /
  ``appdb_schema``）、任何写操作。

表结构事实以 ``apps/backend/app/storage/model`` 的 ORM 定义为准；本脚本子命令与字段随该
目录演进，不硬编码表名清单（``schema`` 子命令从在线库读取真实结构）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import triage_paths as paths
from appdb_agent_facts import (
    list_models,
    list_providers,
    recent_commands,
    recent_runs,
    recent_tasks,
    recent_workspaces,
)
from appdb_context import list_messages, summarize_tool_calls
from appdb_health import find_unsettled
from appdb_readonly import list_tables, open_readonly, resolve_db_path
from appdb_schema import database_overview, table_detail
from appdb_side_effects import list_delegations, list_terminal_sessions
from appdb_snapshots import run_snapshot, task_snapshot

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 10000
_ORDERS = ("asc", "desc")
_DEFAULT_MAX_CHARS = 200


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。

    参数:
        无。

    返回:
        配置好的 ``ArgumentParser``，子命令覆盖结构、事实、上下文、副作用与体检五类查询。

    异常:
        无。

    副作用:
        无。
    """

    parser = argparse.ArgumentParser(
        prog="query_app_db",
        description="只读查询 coding-agent 本地业务库 storage/app.sqlite3。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    schema = subparsers.add_parser("schema", help="查看库内真实表清单、行数与列定义。")
    _add_common_options(schema)
    schema.add_argument("--table", default="", help="只看单张表的详细列定义。")
    schema.add_argument("--no-columns", action="store_true", help="只输出表名与行数。")

    workspaces = subparsers.add_parser("workspaces", help="列出工作区。")
    _add_common_options(workspaces)
    _add_limit(workspaces)

    tasks = subparsers.add_parser("tasks", help="列出任务及其当前运行状态。")
    _add_common_options(tasks)
    _add_limit(tasks)
    tasks.add_argument("--workspace-id", type=int, default=None)
    tasks.add_argument("--contains", default="", help="按标题或任务 id 模糊搜索。")

    runs = subparsers.add_parser("runs", help="列出运行（一次 Agent 执行）。")
    _add_common_options(runs)
    _add_limit(runs)
    runs.add_argument("--task-id", type=int, default=None)
    runs.add_argument("--status", default="", help="pending/running/completed/failed/cancelled。")
    runs.add_argument("--contains", default="", help="按输入/终态原因/最终回复/错误模糊搜索。")

    run = subparsers.add_parser("run", help="查看单次运行的排障快照。")
    _add_common_options(run)
    _add_limit(run)
    run.add_argument("run_id", type=int)

    task = subparsers.add_parser("task", help="查看单个任务的排障快照。")
    _add_common_options(task)
    _add_limit(task)
    task.add_argument("task_id", type=int)

    commands = subparsers.add_parser("commands", help="列出 Transport 命令（幂等占用）。")
    _add_common_options(commands)
    _add_limit(commands)
    commands.add_argument("--task-id", type=int, default=None)
    commands.add_argument("--run-id", type=int, default=None)

    messages = subparsers.add_parser("messages", help="按顺序回放会话上下文消息。")
    _add_common_options(messages)
    _add_limit(messages)
    messages.add_argument("task_id", type=int)
    messages.add_argument("--run-id", type=int, default=None)
    messages.add_argument("--order", choices=_ORDERS, default="asc")
    messages.add_argument("--exclude-streaming", action="store_true", help="排除流式草稿行。")

    tools = subparsers.add_parser("tools", help="汇总工具调用与结果的配对结局。")
    _add_common_options(tools)
    _add_limit(tools)
    tools.add_argument("task_id", type=int)
    tools.add_argument("--run-id", type=int, default=None)
    tools.add_argument("--contains", default="", help="按工具名/参数/结果模糊搜索。")
    tools.add_argument("--failures-only", action="store_true", help="只显示失败调用。")

    delegations = subparsers.add_parser("delegations", help="列出子 Agent 委派。")
    _add_common_options(delegations)
    _add_limit(delegations)
    delegations.add_argument("--task-id", type=int, default=None)

    sessions = subparsers.add_parser("sessions", help="列出终端会话。")
    _add_common_options(sessions)
    _add_limit(sessions)
    sessions.add_argument("--task-id", type=int, default=None)
    sessions.add_argument("--status", default="")

    providers = subparsers.add_parser("providers", help="列出模型厂商（不含明文 Key）。")
    _add_common_options(providers)
    _add_limit(providers)

    models = subparsers.add_parser("models", help="列出模型条目。")
    _add_common_options(models)
    _add_limit(models)
    models.add_argument("--provider-id", type=int, default=None)

    stuck = subparsers.add_parser("stuck", help="体检未收敛的 run / task / 流式草稿。")
    _add_common_options(stuck)
    _add_limit(stuck)

    return parser


def _add_limit(sub: argparse.ArgumentParser) -> None:
    """为子命令追加 ``--limit``。

    参数:
        sub: 子命令解析器。

    返回:
        无。

    异常:
        无。

    副作用:
        向子命令注册参数。
    """

    sub.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help="最大返回行数。")


def _add_common_options(sub: argparse.ArgumentParser) -> None:
    """为子命令追加通用连接与输出选项。

    参数:
        sub: 子命令解析器。

    返回:
        无。

    异常:
        无。

    副作用:
        向子命令注册 ``--db`` / ``--format`` / ``--save`` / ``--force``。
    """

    sub.add_argument("--db", default="", help="业务 SQLite 路径，默认 storage/app.sqlite3。")
    sub.add_argument("--format", choices=("text", "json"), default="text", help="输出格式。")
    sub.add_argument("--save", default="", help="把结果写入指定文件（UTF-8）。")
    sub.add_argument("--force", action="store_true", help="允许 --save 覆盖已有文件。")


def normalize_limit(limit: int) -> int:
    """校验返回行数上限。

    参数:
        limit: 命令行传入的 limit。

    返回:
        合法 limit。

    异常:
        ValueError: limit 小于 1 或超过上限。

    副作用:
        无。
    """

    if limit < 1:
        raise ValueError("limit must be greater than zero")
    if limit > _MAX_LIMIT:
        raise ValueError(f"limit must be less than or equal to {_MAX_LIMIT}")
    return limit


def dispatch(connection: sqlite3.Connection, args: argparse.Namespace) -> Any:
    """把已解析的子命令分发到对应查询。

    参数:
        connection: 只读 SQLite 连接。
        args: argparse 解析后的命名空间。

    返回:
        查询结果（列表或字典），由调用方渲染。

    异常:
        ValueError: 子命令未知或参数非法。
        sqlite3.Error: 查询失败。

    副作用:
        无（仅读库）。
    """

    limit = normalize_limit(args.limit) if hasattr(args, "limit") else _DEFAULT_LIMIT
    handlers: dict[str, Callable[[], Any]] = {
        "schema": lambda: (
            table_detail(connection, args.table.strip())
            if args.table.strip()
            else database_overview(connection, include_columns=not args.no_columns)
        ),
        "workspaces": lambda: recent_workspaces(connection, limit=limit),
        "tasks": lambda: recent_tasks(
            connection, limit=limit, workspace_id=args.workspace_id, contains=args.contains.strip()
        ),
        "runs": lambda: recent_runs(
            connection,
            limit=limit,
            task_id=args.task_id,
            status=args.status.strip(),
            contains=args.contains.strip(),
        ),
        "run": lambda: run_snapshot(connection, run_id=args.run_id, limit=limit),
        "task": lambda: task_snapshot(connection, task_id=args.task_id, limit=limit),
        "commands": lambda: recent_commands(
            connection, limit=limit, task_id=args.task_id, run_id=args.run_id
        ),
        "messages": lambda: list_messages(
            connection,
            task_id=args.task_id,
            run_id=args.run_id,
            limit=limit,
            order=args.order,
            include_streaming=not args.exclude_streaming,
        ),
        "tools": lambda: summarize_tool_calls(
            connection,
            task_id=args.task_id,
            run_id=args.run_id,
            contains=args.contains.strip(),
            failures_only=args.failures_only,
            limit=limit,
        ),
        "delegations": lambda: list_delegations(connection, limit=limit, task_id=args.task_id),
        "sessions": lambda: list_terminal_sessions(
            connection, limit=limit, task_id=args.task_id, status=args.status.strip()
        ),
        "providers": lambda: list_providers(connection, limit=limit),
        "models": lambda: list_models(connection, limit=limit, provider_id=args.provider_id),
        "stuck": lambda: find_unsettled(connection, limit=limit),
    }
    handler = handlers.get(args.command)
    if handler is None:
        raise ValueError(f"unknown command: {args.command}")
    return handler()


def compact_text(value: Any, *, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """把任意值压缩为单行短文本。

    参数:
        value: 待渲染的值。
        max_chars: 最大字符数。

    返回:
        折叠空白并截断后的单行文本；``None`` 渲染为空字符串。

    异常:
        无。

    副作用:
        无。
    """

    if value is None:
        return ""
    if isinstance(value, dict | list):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def render_mapping(row: dict[str, Any]) -> str:
    """把一行结果渲染为单行 ``key=value`` 文本。

    参数:
        row: 结果行字典。

    返回:
        跳过空值的单行文本。

    异常:
        无。

    副作用:
        无。
    """

    parts = []
    for key, value in row.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={compact_text(value)}")
    return " ".join(parts)


def render(value: Any) -> str:
    """把查询结果渲染为适合终端阅读的多行文本。

    参数:
        value: 查询结果（列表或字典）。

    返回:
        多行文本。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(value, list):
        if not value:
            return "(no rows)"
        return "\n".join(render_mapping(row) for row in value)
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            lines.append(f"[{key}]")
            if isinstance(item, list):
                if item:
                    lines.extend(render_mapping(row) for row in item)
                else:
                    lines.append("(none)")
            elif isinstance(item, dict):
                lines.append(render_mapping(item) or "(none)")
            else:
                lines.append(compact_text(item) or "(none)")
        return "\n".join(lines)
    return compact_text(value)


def emit(value: Any, *, output_format: str) -> None:
    """输出渲染结果。

    参数:
        value: 查询结果。
        output_format: ``text`` 或 ``json``。

    返回:
        无。

    异常:
        ValueError: 输出格式非法。

    副作用:
        写 stdout。
    """

    if output_format == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if output_format == "text":
        print(render(value))
        return
    raise ValueError("format must be text or json")


def save_output(path: Path, value: Any, *, output_format: str, force: bool) -> None:
    """把结果写入文件（UTF-8），绕开 Windows 控制台编码。

    参数:
        path: 输出文件路径。
        value: 查询结果。
        output_format: ``text`` 或 ``json``。
        force: 是否允许覆盖已有文件。

    返回:
        无。

    异常:
        FileExistsError: 目标文件已存在且未指定 ``--force``。
        OSError: 创建目录或写文件失败。
        ValueError: 输出格式非法。

    副作用:
        创建父目录并写入文件。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(f"output file already exists: {path}")
    if output_format == "json":
        content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    elif output_format == "text":
        content = render(value) + "\n"
    else:
        raise ValueError("format must be text or json")
    path.write_text(content, encoding="utf-8")


def force_utf8_streams() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 控制台代码页导致输出崩溃。

    参数:
        无。

    返回:
        无。

    异常:
        无（不支持 ``reconfigure`` 的流直接跳过）。

    副作用:
        修改进程标准流的编码设置。
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _announce_db_path(db_path: Path, *, explicit: bool) -> None:
    """把本次查询的数据根与业务库路径写到 stderr。

    排查时最常见的事故是「查了错的那份 ``.cosir``」（桌面应用与直跑后端各有一份
    ``<数据根>/.cosir/storage/app.sqlite3``），故每次运行都明确告知来源；写 stderr 以免
    污染 stdout 的机器可读输出。

    参数:
        db_path: 本次实际使用的业务库路径。
        explicit: 是否由 ``--db`` 显式指定。

    返回:
        无。

    异常:
        无。

    副作用:
        写一至两行到 stderr。
    """

    if explicit:
        print(f"[log-triage] db (explicit): {db_path}", file=sys.stderr)
        return
    print(f"[log-triage] {paths.describe_path_choice()}", file=sys.stderr)
    print(f"[log-triage] db: {db_path}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    参数:
        argv: 命令行参数列表；省略时使用 ``sys.argv[1:]``。

    返回:
        进程退出码：0 成功，1 用户错误或查询失败。

    异常:
        无。

    副作用:
        读取 SQLite；写 stdout/stderr；可选写 ``--save`` 文件。
    """

    force_utf8_streams()
    args = build_parser().parse_args(argv)
    connection: sqlite3.Connection | None = None
    try:
        db_path = resolve_db_path(args.db)
        _announce_db_path(db_path, explicit=bool(args.db.strip()))
        connection = open_readonly(db_path)
        result = dispatch(connection, args)
        if args.save.strip():
            save_output(
                Path(args.save).expanduser(),
                result,
                output_format=args.format,
                force=args.force,
            )
        emit(result, output_format=args.format)
    except sqlite3.OperationalError as exc:
        # 缺表/缺列一律附上库内真实表清单，避免只能拿到裸 SQLite 报错而无法判断 schema 漂移。
        print(f"error: {exc}{_table_inventory_hint(connection)}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError, FileExistsError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()
    return 0


def _table_inventory_hint(connection: sqlite3.Connection | None) -> str:
    """为 SQLite 运行期错误补充库内表清单提示。

    参数:
        connection: 已建立的只读连接；为 None 时无法探测。

    返回:
        形如 ``"; tables in this database: a, b"`` 的提示；无法探测时为空字符串。

    异常:
        无（探测失败静默降级为空提示）。

    副作用:
        无（只读 schema）。
    """

    if connection is None:
        return ""
    try:
        return f"; tables in this database: {', '.join(list_tables(connection))}"
    except sqlite3.Error:
        return ""


if __name__ == "__main__":
    raise SystemExit(main())
