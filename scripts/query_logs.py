#!/usr/bin/env python3
"""日志查询命令行工具（CLI）。

单一职责：以只读方式直连独立日志 SQLite 库（``storage/logs.sqlite3`` 的 ``log_entries`` 表），
按条件过滤并渲染日志，供开发者在终端快速排查问题。

职责边界：
- 负责：定位日志库文件、构造只读查询、按 trace_id / level / event / 时间范围过滤与排序、
  以纯文本或 JSON 渲染输出、参数校验。
- 不负责：日志写入 / 采集 / 格式化（见 ``app/config/logging``）、schema 初始化与迁移
  （见 ``app/storage/init_schema``）、后端应用装配（本脚本刻意不导入 ``app.*``，
  仅用标准库 ``sqlite3`` 直查，避免触发后端初始化链路）。

设计约定：只读打开数据库（``mode=ro`` URI），永不建表、永不写入；表结构以
``app/storage/model/log_model.py`` 的 ``log_entries`` 为稳定事实基线。

用法示例::

    python scripts/query_logs.py recent --limit 50
    python scripts/query_logs.py recent --level ERROR --since 2026-08-01T00:00:00Z
    python scripts/query_logs.py trace <trace_id>
    python scripts/query_logs.py recent --format json --db /path/to/logs.sqlite3
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_ORDERS = ("asc", "desc")
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 10000

# ``log_entries`` 表列，顺序与 SELECT 保持一致（见 app/storage/model/log_model.py）。
_COLUMNS = (
    "ts",
    "level",
    "logger",
    "trace_id",
    "caller",
    "event",
    "msg",
    "data_json",
    "error_json",
    "truncated",
)


def default_db_path() -> Path:
    """推导默认日志库文件路径。

    从本脚本位置向上定位仓库根（``scripts/`` 的上一级），拼出
    ``<root>/storage/logs.sqlite3``；与 ``Settings.load`` 的默认推导一致。

    参数:
        无。

    返回:
        默认日志库文件路径（未保证存在）。

    异常:
        无。

    副作用:
        无。
    """

    repository_root = Path(__file__).resolve().parent.parent
    return repository_root / "storage" / "logs.sqlite3"


def normalize_level(level: str) -> str:
    """校验并归一化日志级别过滤参数。

    参数:
        level: 原始级别文本，允许为空。

    返回:
        大写后的标准日志级别；空字符串表示不筛选。

    异常:
        ValueError: 如果级别不受支持。

    副作用:
        无。
    """

    normalized = level.strip().upper()
    if not normalized:
        return ""
    if normalized == "WARN":
        normalized = "WARNING"
    if normalized not in _LEVELS:
        raise ValueError(f"level must be one of {', '.join(_LEVELS)}")
    return normalized


def normalize_time(value: str) -> str:
    """校验并归一化 UTC RFC3339 时间。

    参数:
        value: 原始时间文本，允许为空；接受尾随 ``Z`` 或带时区偏移的 ISO 8601。

    返回:
        空字符串或毫秒精度 UTC RFC3339 文本（形如 ``2026-08-01T00:00:00.000Z``）。

    异常:
        ValueError: 如果时间文本非法。

    副作用:
        无。
    """

    if not value.strip():
        return ""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_limit(limit: int) -> int:
    """校验 limit 是否在合法区间。

    参数:
        limit: 调用方传入的最大返回数量。

    返回:
        合法 limit。

    异常:
        ValueError: 如果 limit 小于 1 或大于上限。

    副作用:
        无。
    """

    if limit < 1:
        raise ValueError("limit must be greater than zero")
    if limit > _MAX_LIMIT:
        raise ValueError(f"limit must be less than or equal to {_MAX_LIMIT}")
    return limit


def build_query(
    *,
    trace_id: str = "",
    level: str = "",
    event: str = "",
    start_time: str = "",
    end_time: str = "",
    limit: int = _DEFAULT_LIMIT,
    order: str = "asc",
) -> tuple[str, list[Any]]:
    """构造只读 SELECT 语句与参数列表。

    仅对非空过滤条件追加 WHERE：trace_id / level / event 精确匹配，
    start_time / end_time 限定 ts 范围；排序以 ts 为主、id 为辅（同方向），并应用 limit。
    使用参数占位符，杜绝 SQL 注入。

    参数:
        trace_id: 可选 trace 过滤条件。
        level: 可选日志级别（已归一化）。
        event: 可选事件名过滤条件。
        start_time: 可选 UTC RFC3339 起始时间（已归一化）。
        end_time: 可选 UTC RFC3339 结束时间（已归一化）。
        limit: 已校验的最大返回数量。
        order: 排序方向，``asc`` 或 ``desc``。

    返回:
        ``(sql, params)`` 二元组，可直接传入 ``sqlite3.Connection.execute``。

    异常:
        ValueError: 如果 order 非 asc/desc。

    副作用:
        无。
    """

    if order not in _ORDERS:
        raise ValueError("order must be asc or desc")

    where: list[str] = []
    params: list[Any] = []
    for column, value in (("trace_id", trace_id), ("level", level), ("event", event)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if start_time:
        where.append("ts >= ?")
        params.append(start_time)
    if end_time:
        where.append("ts <= ?")
        params.append(end_time)

    direction = "ASC" if order == "asc" else "DESC"
    # 列名来自模块级常量 _COLUMNS，非用户输入；过滤值全部走 ? 占位符，无注入风险。
    sql = f"SELECT {', '.join(_COLUMNS)} FROM log_entries"  # noqa: S608
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY ts {direction}, id {direction} LIMIT ?"
    params.append(limit)
    return sql, params


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读模式打开日志库连接。

    使用 ``file:...?mode=ro`` URI，确保脚本绝不建表、绝不写入；文件不存在时抛错。

    参数:
        db_path: 日志库文件路径。

    返回:
        只读 SQLite 连接（行工厂设为 ``sqlite3.Row``）。

    异常:
        FileNotFoundError: 如果数据库文件不存在。
        sqlite3.OperationalError: 如果无法以只读模式打开。

    副作用:
        打开一个 SQLite 连接（调用方负责关闭）。
    """

    if not db_path.exists():
        raise FileNotFoundError(f"log database not found: {db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    """把数据库行转换为结构化日志字典。

    ``data_json`` 反序列化后若不是 dict 则回退为空 dict；``error_json`` 为空时 error 记为
    None，非 dict 时同样回退为 None；可空文本列（trace_id/caller）为 None 时回退为空字符串。

    参数:
        row: 查询得到的行（``sqlite3.Row``）。

    返回:
        含 10 个统一日志字段的字典。

    异常:
        无（JSON 解析失败时该字段回退为默认值，不中断整体查询）。

    副作用:
        无。
    """

    data = _safe_json(row["data_json"], default={})
    error = _safe_json(row["error_json"], default=None) if row["error_json"] else None
    return {
        "ts": row["ts"],
        "level": row["level"],
        "logger": row["logger"],
        "trace_id": row["trace_id"] or "",
        "caller": row["caller"] or "",
        "event": row["event"],
        "msg": row["msg"],
        "data": data if isinstance(data, dict) else {},
        "error": error if isinstance(error, dict) else None,
        "truncated": bool(row["truncated"]),
    }


def _safe_json(raw: str | None, *, default: Any) -> Any:
    """宽容地反序列化 JSON 文本，失败时返回默认值。

    参数:
        raw: 待解析的 JSON 文本，允许为 None。
        default: 解析失败或输入为空时的回退值。

    返回:
        解析结果或默认值。

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


def render_text(entries: list[dict[str, Any]]) -> str:
    """把结构化日志渲染为多行纯文本。

    与后端 ``LogQueryService.render_log_entries`` 保持一致的可读格式：
    ``data`` / ``error`` 以扁平 ``key=value`` 片段展示，不含花括号。

    参数:
        entries: 待渲染的日志字典列表。

    返回:
        多行纯文本日志；没有记录时返回空字符串。

    异常:
        无。

    副作用:
        无。
    """

    return "\n".join(_render_entry(entry) for entry in entries)


def _render_entry(entry: dict[str, Any]) -> str:
    """渲染单条结构化日志为一行文本。

    参数:
        entry: 待渲染的日志字典。

    返回:
        单行日志文本。

    异常:
        无。

    副作用:
        无。
    """

    parts = [entry["ts"], entry["level"], entry["logger"], f"event={entry['event']}"]
    if entry["trace_id"]:
        parts.append(f"trace_id={entry['trace_id']}")
    if entry["caller"]:
        parts.append(f"caller={entry['caller']}")
    parts.append(f'msg="{entry["msg"]}"')
    if entry["data"]:
        parts.append(" ".join(_flatten_kv("data", entry["data"])))
    if entry["error"]:
        parts.append(" ".join(_flatten_kv("error", entry["error"])))
    return " ".join(parts)


def _flatten_kv(prefix: str, value: Any) -> list[str]:
    """把字典/列表递归扁平化为 ``key=value`` 片段，避免花括号。

    参数:
        prefix: 当前层级键前缀（如 ``data`` 或 ``data.user``）。
        value: 待扁平化的任意值。

    返回:
        扁平化后的 ``key=value`` 片段列表；空容器返回空列表。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.extend(_flatten_kv(f"{prefix}.{key}" if prefix else str(key), item))
        return out
    if isinstance(value, list | tuple):
        out = []
        for index, item in enumerate(value):
            out.extend(_flatten_kv(f"{prefix}[{index}]", item))
        return out
    return [f"{prefix}={value}"]


def run_query(db_path: Path, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    """执行只读查询并把结果行转换为结构化字典列表。

    参数:
        db_path: 日志库文件路径。
        sql: 已构造的 SELECT 语句。
        params: 与 SELECT 占位符对应的参数列表。

    返回:
        结构化日志字典列表；无匹配时为空列表。

    异常:
        FileNotFoundError: 如果数据库文件不存在。
        sqlite3.Error: 如果查询失败。

    副作用:
        打开并关闭一个只读 SQLite 连接。
    """

    connection = open_readonly(db_path)
    try:
        rows = connection.execute(sql, params).fetchall()
    finally:
        connection.close()
    return [row_to_entry(row) for row in rows]


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。

    定义 ``recent`` 与 ``trace`` 两个子命令，以及共享的过滤 / 输出选项。

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
        prog="query_logs",
        description="只读查询本地日志 SQLite 库（storage/logs.sqlite3）。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    recent = subparsers.add_parser("recent", help="按时间倒序查询最近日志。")
    _add_shared_options(recent)
    recent.add_argument("--event", default="", help="按事件名精确过滤。")

    trace = subparsers.add_parser("trace", help="按 trace_id 查询完整链路（时间正序）。")
    trace.add_argument("trace_id", help="必填 trace 标识。")
    _add_shared_options(trace)

    return parser


def _add_shared_options(sub: argparse.ArgumentParser) -> None:
    """为子命令追加共享的过滤与输出选项。

    参数:
        sub: 目标子命令解析器。

    返回:
        无。

    异常:
        无。

    副作用:
        向 ``sub`` 注册命令行参数。
    """

    sub.add_argument("--level", default="", help="日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）。")
    sub.add_argument(
        "--since", default="", help="起始时间（UTC RFC3339，如 2026-08-01T00:00:00Z）。"
    )
    sub.add_argument("--until", default="", help="结束时间（UTC RFC3339）。")
    sub.add_argument(
        "--limit", type=int, default=_DEFAULT_LIMIT, help=f"最大返回数量（默认 {_DEFAULT_LIMIT}）。"
    )
    sub.add_argument(
        "--db", default="", help="日志库文件路径（默认自动定位仓库 storage/logs.sqlite3）。"
    )
    sub.add_argument(
        "--format", choices=("text", "json"), default="text", help="输出格式（默认 text）。"
    )


def resolve_db_path(raw: str) -> Path:
    """解析日志库文件路径参数。

    参数:
        raw: 命令行 ``--db`` 原始值；为空时使用默认推导。

    返回:
        解析后的日志库文件路径。

    异常:
        无。

    副作用:
        无。
    """

    return Path(raw).expanduser() if raw.strip() else default_db_path()


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数、执行查询并输出结果。

    参数:
        argv: 命令行参数列表；省略时使用 ``sys.argv[1:]``（便于测试注入）。

    返回:
        进程退出码：0 成功；1 用户错误（参数/文件/查询失败）。

    异常:
        无（内部异常转为退出码与 stderr 提示）。

    副作用:
        读取日志 SQLite；向 stdout 写查询结果，向 stderr 写错误提示。
    """

    args = build_parser().parse_args(argv)
    try:
        level = normalize_level(args.level)
        start_time = normalize_time(args.since)
        end_time = normalize_time(args.until)
        limit = normalize_limit(args.limit)
        if args.command == "trace":
            trace_id = args.trace_id.strip()
            if not trace_id:
                raise ValueError("trace_id must not be blank")
            sql, params = build_query(
                trace_id=trace_id,
                level=level,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                order="asc",
            )
        else:
            sql, params = build_query(
                level=level,
                event=args.event,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                order="desc",
            )
        entries = run_query(resolve_db_path(args.db), sql, params)
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    else:
        print(render_text(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
