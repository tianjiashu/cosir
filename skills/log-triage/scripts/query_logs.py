#!/usr/bin/env python3
"""本地结构化日志查询 CLI（log-triage skill 内置副本）。

单一职责：以只读方式查询 ``storage/logs.sqlite3`` 的 ``log_entries`` 表，并按开发者
或 Agent 排查问题所需的过滤条件输出日志。脚本刻意不导入 ``app.*``，避免触发后端初始化。

本文件是 ``scripts/query_logs.py`` 的 skill 内置副本；仅修改了 ``default_db_path``
以从任意调用位置定位到真正的仓库根（向上查找包含 ``apps/backend`` 的目录），其余逻辑与
上游保持一致。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_RANK = {level: index for index, level in enumerate(_LEVELS)}
_ORDERS = ("asc", "desc")
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 10000
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


def repository_root() -> Path:
    """向上查找真正的仓库根（包含 ``apps/backend`` 的目录）。

    参数:
        无。
    返回:
        仓库根绝对路径。
    异常:
        无。
    副作用:
        解析当前脚本路径。
    """

    current = Path(__file__).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / "apps" / "backend").exists():
            return candidate
    # 兜底：退回到脚本两级之上的旧假设（与原脚本行为一致），避免在无仓库结构的场景下崩溃。
    return Path(__file__).resolve().parent.parent


def default_db_path() -> Path:
    """推导默认日志数据库路径。

    参数:
        无。
    返回:
        仓库根目录下 ``storage/logs.sqlite3`` 的路径。
    异常:
        无。
    副作用:
        解析当前脚本路径。
    """

    return repository_root() / "storage" / "logs.sqlite3"


def normalize_level(level: str) -> str:
    """校验并归一化日志级别。

    参数:
        level: 原始日志级别文本，允许为空。
    返回:
        大写日志级别；空字符串表示不按精确级别过滤。
    异常:
        ValueError: 如果日志级别不受支持。
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


def normalize_min_level(level: str, *, errors_only: bool, warnings_up: bool) -> str:
    """归一化最低日志级别快捷参数。

    参数:
        level: 显式最低级别文本。
        errors_only: 是否只返回 ERROR 及以上。
        warnings_up: 是否返回 WARNING 及以上。
    返回:
        最低日志级别；空字符串表示不按最低级别过滤。
    异常:
        ValueError: 如果多个最低级别过滤同时启用，或级别非法。
    副作用:
        无。
    """

    enabled_count = sum(bool(item) for item in (level.strip(), errors_only, warnings_up))
    if enabled_count > 1:
        raise ValueError("use only one of --min-level, --errors-only, or --warnings-up")
    if errors_only:
        return "ERROR"
    if warnings_up:
        return "WARNING"
    return normalize_level(level)


def normalize_time(value: str) -> str:
    """校验并归一化 UTC RFC3339 时间。

    参数:
        value: 原始时间文本，允许为空；接受尾随 ``Z`` 或带时区偏移的 ISO 8601。
    返回:
        空字符串或毫秒精度 UTC RFC3339 文本。
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


def normalize_duration(value: str) -> timedelta:
    """校验并解析简短时间窗口。

    参数:
        value: 时间窗口文本，支持 ``s``、``m``、``h`` 后缀，例如 ``90s``、``2m``、``1h``。
    返回:
        解析后的 ``timedelta``。
    异常:
        ValueError: 如果格式非法或数值不大于 0。
    副作用:
        无。
    """

    raw = value.strip().lower()
    if len(raw) < 2:
        raise ValueError("window must look like 90s, 2m, or 1h")
    unit = raw[-1]
    number_text = raw[:-1]
    if unit not in {"s", "m", "h"}:
        raise ValueError("window unit must be s, m, or h")
    try:
        amount = int(number_text)
    except ValueError as exc:
        raise ValueError("window amount must be an integer") from exc
    if amount < 1:
        raise ValueError("window amount must be greater than zero")
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    return timedelta(hours=amount)


def normalize_around_window(around: str, window: str) -> tuple[str, str]:
    """根据中心时间和窗口宽度生成起止时间。

    参数:
        around: 中心时间，接受 ``normalize_time`` 支持的 ISO 8601 文本。
        window: 单侧窗口宽度，支持 ``s``、``m``、``h`` 后缀。
    返回:
        ``(start_time, end_time)``，均为 UTC RFC3339 文本。
    异常:
        ValueError: 如果 around 或 window 非法。
    副作用:
        无。
    """

    normalized = normalize_time(around)
    center = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    delta = normalize_duration(window)
    start_time = (center - delta).astimezone(UTC).isoformat(timespec="milliseconds")
    end_time = (center + delta).astimezone(UTC).isoformat(timespec="milliseconds")
    return start_time.replace("+00:00", "Z"), end_time.replace("+00:00", "Z")


def normalize_limit(limit: int) -> int:
    """校验返回数量上限。

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


def build_query(
    *,
    trace_id: str = "",
    level: str = "",
    min_level: str = "",
    event: str = "",
    event_prefix: str = "",
    caller_contains: str = "",
    logger_contains: str = "",
    contains: str = "",
    start_time: str = "",
    end_time: str = "",
    limit: int = _DEFAULT_LIMIT,
    order: str = "asc",
) -> tuple[str, list[Any]]:
    """构造只读 SELECT 语句与参数列表。

    参数:
        trace_id: 可选 trace 过滤条件。
        level: 可选日志级别精确过滤条件。
        min_level: 可选最低日志级别过滤条件。
        event: 可选事件名精确过滤条件。
        event_prefix: 可选事件名前缀过滤条件。
        caller_contains: 可选 caller 模糊过滤条件。
        logger_contains: 可选 logger 模糊过滤条件。
        contains: 可选全文片段过滤条件。
        start_time: 可选 UTC RFC3339 起始时间。
        end_time: 可选 UTC RFC3339 结束时间。
        limit: 已校验的最大返回数量。
        order: 排序方向，``asc`` 或 ``desc``。
    返回:
        ``(sql, params)``，可传给 ``sqlite3.Connection.execute``。
    异常:
        ValueError: 如果 order 或 min_level 非法。
    副作用:
        无。
    """

    if order not in _ORDERS:
        raise ValueError("order must be asc or desc")
    if min_level and min_level not in _LEVEL_RANK:
        raise ValueError(f"min_level must be one of {', '.join(_LEVELS)}")

    where: list[str] = []
    params: list[Any] = []
    for column, value in (("trace_id", trace_id), ("level", level), ("event", event)):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if min_level:
        allowed_levels = _LEVELS[_LEVEL_RANK[min_level] :]
        where.append(f"level IN ({', '.join('?' for _ in allowed_levels)})")
        params.extend(allowed_levels)
    if event_prefix:
        where.append("event LIKE ? ESCAPE '\\'")
        params.append(f"{escape_like(event_prefix)}%")
    for column, value in (("caller", caller_contains), ("logger", logger_contains)):
        if value:
            where.append(f"{column} LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(value)}%")
    if contains:
        where.append(
            "("
            "event LIKE ? ESCAPE '\\' OR msg LIKE ? ESCAPE '\\' "
            "OR caller LIKE ? ESCAPE '\\' OR logger LIKE ? ESCAPE '\\' "
            "OR data_json LIKE ? ESCAPE '\\' OR error_json LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([f"%{escape_like(contains)}%"] * 6)
    if start_time:
        where.append("ts >= ?")
        params.append(start_time)
    if end_time:
        where.append("ts <= ?")
        params.append(end_time)

    direction = "ASC" if order == "asc" else "DESC"
    sql = f"SELECT {', '.join(_COLUMNS)} FROM log_entries"  # noqa: S608
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY ts {direction}, id {direction} LIMIT ?"
    params.append(limit)
    return sql, params


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读模式打开日志库连接。

    参数:
        db_path: 日志库文件路径。
    返回:
        只读 SQLite 连接，行工厂为 ``sqlite3.Row``。
    异常:
        FileNotFoundError: 如果数据库文件不存在。
        sqlite3.OperationalError: 如果无法以只读模式打开。
    副作用:
        打开一个 SQLite 连接。
    """

    if not db_path.exists():
        raise FileNotFoundError(f"log database not found: {db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    """把数据库行转换为结构化日志字典。

    参数:
        row: 查询得到的 ``sqlite3.Row``。
    返回:
        包含统一日志字段的字典。
    异常:
        无。
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
    """宽容地反序列化 JSON 文本。

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
    """把结构化日志渲染为多行文本。

    参数:
        entries: 待渲染的日志字典列表。
    返回:
        多行文本；无记录时为空字符串。
    异常:
        无。
    副作用:
        无。
    """

    return "\n".join(_render_entry(entry) for entry in entries)


def _render_entry(entry: dict[str, Any]) -> str:
    """渲染单条结构化日志。

    参数:
        entry: 待渲染的日志字典。
    返回:
        单行文本。
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
    """把嵌套对象扁平化为 ``key=value`` 片段。

    参数:
        prefix: 当前层级键前缀。
        value: 待扁平化的任意值。
    返回:
        扁平化后的文本片段列表。
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
    """执行只读查询并转换结果行。

    参数:
        db_path: 日志库文件路径。
        sql: 已构造的 SELECT 语句。
        params: SELECT 占位符参数列表。
    返回:
        结构化日志字典列表。
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


def resolve_db_path(raw: str) -> Path:
    """解析日志库文件路径参数。

    参数:
        raw: 命令行 ``--db`` 原始值；为空时使用默认路径。
    返回:
        解析后的数据库文件路径。
    异常:
        无。
    副作用:
        无。
    """

    return Path(raw).expanduser() if raw.strip() else default_db_path()


def resolve_time_range(args: argparse.Namespace) -> tuple[str, str]:
    """解析命令行时间范围参数。

    参数:
        args: argparse 解析后的命名空间。
    返回:
        ``(start_time, end_time)``，空字符串表示未指定边界。
    异常:
        ValueError: 如果 around 与 since/until 混用，或时间格式非法。
    副作用:
        无。
    """

    if args.around.strip():
        if args.since.strip() or args.until.strip():
            raise ValueError("--around cannot be combined with --since or --until")
        return normalize_around_window(args.around, args.window)
    return normalize_time(args.since), normalize_time(args.until)


def write_output_file(
    output_path: Path,
    entries: list[dict[str, Any]],
    *,
    output_format: str,
    force: bool = False,
) -> Path:
    """将查询结果保存到本地文件。

    参数:
        output_path: 输出文件路径。
        entries: 查询得到的结构化日志条目。
        output_format: 保存格式；支持 ``json`` 或 ``text``。
        force: 是否允许覆盖已有文件。
    返回:
        实际写入的文件路径。
    异常:
        ValueError: 如果输出格式非法。
        OSError: 如果创建目录或写文件失败。
    副作用:
        创建父目录并写入文件。
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and not force:
        raise FileExistsError(f"output file already exists: {output_path}")
    if output_format == "json":
        content = json.dumps(entries, ensure_ascii=False, indent=2) + "\n"
    elif output_format == "text":
        content = render_text(entries) + "\n"
    else:
        raise ValueError("output_format must be text or json")
    output_path.write_text(content, encoding="utf-8")
    return output_path


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
        prog="query_logs",
        description="只读查询本地结构化日志 SQLite。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    recent = subparsers.add_parser("recent", help="按时间倒序查询最近日志。")
    _add_shared_options(recent)
    recent.add_argument("--event", default="", help="按事件名精确过滤。")

    trace = subparsers.add_parser("trace", help="按 trace_id 查询完整链路。")
    trace.add_argument("trace_id", help="必填 trace 标识。")
    _add_shared_options(trace)

    return parser


def _add_shared_options(sub: argparse.ArgumentParser) -> None:
    """为子命令追加共享过滤和输出选项。

    参数:
        sub: 目标子命令解析器。
    返回:
        无。
    异常:
        无。
    副作用:
        向 ``sub`` 注册命令行参数。
    """

    sub.add_argument("--level", default="", help="日志级别精确过滤。")
    sub.add_argument("--min-level", default="", help="最低日志级别过滤。")
    sub.add_argument("--errors-only", action="store_true", help="只返回 ERROR 及以上日志。")
    sub.add_argument("--warnings-up", action="store_true", help="返回 WARNING 及以上日志。")
    sub.add_argument(
        "--contains",
        default="",
        help="在 event/msg/caller/logger/data/error 中搜索。",
    )
    sub.add_argument("--event-prefix", default="", help="按 event 前缀过滤。")
    sub.add_argument("--caller-contains", default="", help="按 caller 片段过滤。")
    sub.add_argument("--logger-contains", default="", help="按 logger 片段过滤。")
    sub.add_argument("--since", default="", help="起始时间，UTC RFC3339。")
    sub.add_argument("--until", default="", help="结束时间，UTC RFC3339。")
    sub.add_argument("--around", default="", help="中心时间；配合 --window 查询前后日志。")
    sub.add_argument("--window", default="2m", help="--around 的单侧窗口，支持 90s/2m/1h。")
    sub.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help="最大返回数量。")
    sub.add_argument("--db", default="", help="日志 SQLite 文件路径。")
    sub.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="输出格式。",
    )
    sub.add_argument("--save", default="", help="将查询结果保存到指定文件。")
    sub.add_argument("--force", action="store_true", help="允许 --save 覆盖已有文件。")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数、执行查询并输出结果。

    参数:
        argv: 命令行参数列表；省略时使用 ``sys.argv[1:]``。
    返回:
        进程退出码，0 表示成功，1 表示用户错误或查询失败。
    异常:
        无。
    副作用:
        读取 SQLite；向 stdout/stderr 写文本；可选写保存文件。
    """

    args = build_parser().parse_args(argv)
    try:
        level = normalize_level(args.level)
        min_level = normalize_min_level(
            args.min_level,
            errors_only=args.errors_only,
            warnings_up=args.warnings_up,
        )
        start_time, end_time = resolve_time_range(args)
        limit = normalize_limit(args.limit)
        shared_filters = {
            "level": level,
            "min_level": min_level,
            "event_prefix": args.event_prefix.strip(),
            "caller_contains": args.caller_contains.strip(),
            "logger_contains": args.logger_contains.strip(),
            "contains": args.contains.strip(),
            "start_time": start_time,
            "end_time": end_time,
            "limit": limit,
        }
        if args.command == "trace":
            trace_id = args.trace_id.strip()
            if not trace_id:
                raise ValueError("trace_id must not be blank")
            sql, params = build_query(trace_id=trace_id, order="asc", **shared_filters)
        else:
            sql, params = build_query(
                event=args.event.strip(),
                order="desc",
                **shared_filters,
            )
        entries = run_query(resolve_db_path(args.db), sql, params)
        if args.save.strip():
            write_output_file(
                Path(args.save).expanduser(),
                entries,
                output_format=args.format,
                force=args.force,
            )
    except (ValueError, FileNotFoundError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    else:
        print(render_text(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
