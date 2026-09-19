#!/usr/bin/env python3
"""只读查询本地固定格式 JSONL 日志。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_RANK = {level: index for index, level in enumerate(_LEVELS)}
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 10000


def repository_root() -> Path:
    """向上查找包含 ``apps/backend`` 的仓库根目录。"""
    for start in (Path(__file__).resolve().parent, Path.cwd()):
        for candidate in (start, *start.parents):
            if (candidate / "apps" / "backend").exists():
                return candidate
    raise ValueError("cannot locate repository root; pass --log-file explicitly")


def default_log_dir() -> Path:
    """返回纯后端开发模式的默认日志目录。"""
    return repository_root() / "logs"


def normalize_level(level: str) -> str:
    """校验并归一化精确日志级别。"""
    normalized = level.strip().upper()
    if normalized == "WARN":
        normalized = "WARNING"
    if normalized and normalized not in _LEVELS:
        raise ValueError(f"level must be one of {', '.join(_LEVELS)}")
    return normalized


def normalize_min_level(level: str, *, errors_only: bool, warnings_up: bool) -> str:
    """归一化最低日志级别快捷参数。"""
    if sum(bool(item) for item in (level.strip(), errors_only, warnings_up)) > 1:
        raise ValueError("use only one of --min-level, --errors-only, or --warnings-up")
    if errors_only:
        return "ERROR"
    if warnings_up:
        return "WARNING"
    return normalize_level(level)


def normalize_time(value: str) -> str:
    """校验并归一化 UTC RFC3339 时间。"""
    if not value.strip():
        return ""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_duration(value: str) -> timedelta:
    """解析 ``90s``、``2m`` 或 ``1h`` 形式的时间窗口。"""
    raw = value.strip().lower()
    if len(raw) < 2 or raw[-1] not in {"s", "m", "h"}:
        raise ValueError("window must look like 90s, 2m, or 1h")
    try:
        amount = int(raw[:-1])
    except ValueError as exc:
        raise ValueError("window amount must be an integer") from exc
    if amount < 1:
        raise ValueError("window amount must be greater than zero")
    return {"s": timedelta(seconds=amount), "m": timedelta(minutes=amount), "h": timedelta(hours=amount)}[
        raw[-1]
    ]


def normalize_around_window(around: str, window: str) -> tuple[str, str]:
    """根据中心时间和窗口宽度生成起止时间。"""
    center = datetime.fromisoformat(normalize_time(around).replace("Z", "+00:00"))
    delta = normalize_duration(window)
    return (
        (center - delta).astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        (center + delta).astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    )


def normalize_limit(limit: int) -> int:
    """校验返回数量上限。"""
    if limit < 1 or limit > _MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
    return limit


def resolve_log_files(raw: str) -> list[Path]:
    """解析日志文件或日志目录；目录会读取所有 backend JSONL 分片。"""
    target = Path(raw).expanduser() if raw.strip() else default_log_dir()
    if target.is_file():
        return [target]
    if target.is_dir():
        files = sorted(target.glob("backend-*.log"), key=lambda path: path.stat().st_mtime)
        if files:
            return files
    raise FileNotFoundError(f"log file or directory not found: {target}")


def read_entries(files: list[Path]) -> list[dict[str, Any]]:
    """读取 JSONL 分片，忽略空行和损坏行以保留排查可用性。"""
    entries: list[dict[str, Any]] = []
    for path in files:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entry.setdefault("data", {})
                    entry.setdefault("error", None)
                    entry.setdefault("trace_id", "")
                    entry.setdefault("caller", "")
                    entries.append(entry)
    return entries


def _contains(entry: dict[str, Any], needle: str) -> bool:
    return not needle or needle.casefold() in json.dumps(entry, ensure_ascii=False).casefold()


def filter_entries(
    entries: list[dict[str, Any]],
    *,
    trace_id: str = "",
    event: str = "",
    level: str = "",
    min_level: str = "",
    event_prefix: str = "",
    caller_contains: str = "",
    logger_contains: str = "",
    contains: str = "",
    start_time: str = "",
    end_time: str = "",
    limit: int = _DEFAULT_LIMIT,
    order: str = "asc",
) -> list[dict[str, Any]]:
    """按统一日志字段过滤并排序 JSONL 记录。"""
    if order not in {"asc", "desc"}:
        raise ValueError("order must be asc or desc")
    min_rank = _LEVEL_RANK[min_level] if min_level else -1
    result = []
    for entry in entries:
        entry_level = str(entry.get("level", ""))
        if trace_id and entry.get("trace_id", "") != trace_id:
            continue
        if event and entry.get("event", "") != event:
            continue
        if level and entry_level != level:
            continue
        if min_rank >= 0 and _LEVEL_RANK.get(entry_level, -1) < min_rank:
            continue
        if event_prefix and not str(entry.get("event", "")).startswith(event_prefix):
            continue
        if caller_contains and caller_contains.casefold() not in str(entry.get("caller", "")).casefold():
            continue
        if logger_contains and logger_contains.casefold() not in str(entry.get("logger", "")).casefold():
            continue
        if start_time and str(entry.get("ts", "")) < start_time:
            continue
        if end_time and str(entry.get("ts", "")) > end_time:
            continue
        if not _contains(entry, contains):
            continue
        result.append(entry)
    result.sort(key=lambda item: str(item.get("ts", "")), reverse=order == "desc")
    return result[:limit]


def render_text(entries: list[dict[str, Any]]) -> str:
    """把结构化日志渲染为多行可读文本。"""
    return "\n".join(_render_entry(entry) for entry in entries)


def _render_entry(entry: dict[str, Any]) -> str:
    parts = [
        str(entry.get("ts", "")),
        str(entry.get("level", "")),
        str(entry.get("logger", "")),
        f"event={entry.get('event', '')}",
    ]
    if entry.get("trace_id"):
        parts.append(f"trace_id={entry['trace_id']}")
    if entry.get("caller"):
        parts.append(f"caller={entry['caller']}")
    parts.append(f'msg="{entry.get("msg", "")}"')
    for key in ("data", "error"):
        if entry.get(key):
            parts.append(" ".join(_flatten_kv(key, entry[key])))
    return " ".join(parts)


def _flatten_kv(prefix: str, value: Any) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            result.extend(_flatten_kv(f"{prefix}.{key}", item))
        return result
    if isinstance(value, list | tuple):
        result = []
        for index, item in enumerate(value):
            result.extend(_flatten_kv(f"{prefix}[{index}]", item))
        return result
    return [f"{prefix}={value}"]


def write_output_file(output_path: Path, entries: list[dict[str, Any]], *, output_format: str, force: bool) -> None:
    """将查询结果保存为 JSON 数组或可读文本。"""
    if output_path.exists() and not force:
        raise FileExistsError(f"output file already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(entries, ensure_ascii=False, indent=2) if output_format == "json" else render_text(entries)
    output_path.write_text(content + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="query_logs", description="只读查询本地固定格式 JSONL 日志。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    recent = subparsers.add_parser("recent", help="按时间倒序查询最近日志。")
    _add_shared_options(recent)
    recent.add_argument("--event", default="", help="按事件名精确过滤。")
    trace = subparsers.add_parser("trace", help="按 trace_id 查询完整链路。")
    trace.add_argument("trace_id", help="必填 trace 标识。")
    _add_shared_options(trace)
    return parser


def _add_shared_options(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--level", default="", help="日志级别精确过滤。")
    sub.add_argument("--min-level", default="", help="最低日志级别过滤。")
    sub.add_argument("--errors-only", action="store_true", help="只返回 ERROR 及以上日志。")
    sub.add_argument("--warnings-up", action="store_true", help="返回 WARNING 及以上日志。")
    sub.add_argument("--contains", default="", help="在完整结构化日志中搜索。")
    sub.add_argument("--event-prefix", default="", help="按 event 前缀过滤。")
    sub.add_argument("--caller-contains", default="", help="按 caller 片段过滤。")
    sub.add_argument("--logger-contains", default="", help="按 logger 片段过滤。")
    sub.add_argument("--since", default="", help="起始时间，UTC RFC3339。")
    sub.add_argument("--until", default="", help="结束时间，UTC RFC3339。")
    sub.add_argument("--around", default="", help="中心时间；配合 --window 查询前后日志。")
    sub.add_argument("--window", default="2m", help="--around 的单侧窗口，支持 90s/2m/1h。")
    sub.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help="最大返回数量。")
    sub.add_argument("--log-file", default="", help="日志文件或包含 backend-*.log 分片的目录。")
    sub.add_argument("--format", choices=("text", "json"), default="text", help="输出格式。")
    sub.add_argument("--save", default="", help="将查询结果保存到指定文件。")
    sub.add_argument("--force", action="store_true", help="允许 --save 覆盖已有文件。")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；读取 JSONL、过滤记录并输出结果。"""
    force_utf8_streams()
    args = build_parser().parse_args(argv)
    try:
        start_time = normalize_time(args.since)
        end_time = normalize_time(args.until)
        if args.around.strip():
            if start_time or end_time:
                raise ValueError("--around cannot be combined with --since or --until")
            start_time, end_time = normalize_around_window(args.around, args.window)
        filters = {
            "level": normalize_level(args.level),
            "min_level": normalize_min_level(args.min_level, errors_only=args.errors_only, warnings_up=args.warnings_up),
            "event_prefix": args.event_prefix.strip(),
            "caller_contains": args.caller_contains.strip(),
            "logger_contains": args.logger_contains.strip(),
            "contains": args.contains.strip(),
            "start_time": start_time,
            "end_time": end_time,
            "limit": normalize_limit(args.limit),
        }
        entries = read_entries(resolve_log_files(args.log_file))
        if args.command == "trace":
            trace_id = args.trace_id.strip()
            if not trace_id:
                raise ValueError("trace_id must not be blank")
            entries = filter_entries(entries, trace_id=trace_id, order="asc", **filters)
        else:
            entries = filter_entries(entries, event=args.event.strip(), order="desc", **filters)
        if args.save.strip():
            write_output_file(Path(args.save).expanduser(), entries, output_format=args.format, force=args.force)
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    elif entries:
        print(render_text(entries))
    else:
        print("(no entries)")
    return 0


def force_utf8_streams() -> None:
    """把标准流切到 UTF-8，避免 Windows 控制台代码页导致输出失败。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
