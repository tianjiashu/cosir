#!/usr/bin/env python3
"""Offline analysis tool for local Langfuse replay JSON (log-triage skill bundled copy).

Single responsibility: load a replay saved by ``fetch_langfuse_replay.py`` and summarize it for
LLM-assisted debugging. Commands: summary, timeline, hotspots, generations, inspect, export-llm.

This is a bundled copy of ``scripts/analyze_langfuse_replay.py``; the original has no repo-root
hardcoded paths, so it is copied as-is.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_ORDER = ("summary", "timeline", "hotspots", "generations", "inspect", "export-llm")


def load_replay(replay_path: Path) -> dict[str, Any]:
    """Load and minimally validate a Langfuse replay JSON file.

    Args:
        replay_path: Path to the replay JSON produced by fetch_langfuse_replay.py.

    Returns:
        Parsed replay dictionary.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is not valid JSON or missing required keys.

    Side Effects:
        Reads the file from disk.
    """

    if not replay_path.exists():
        raise FileNotFoundError(f"replay not found: {replay_path}")
    try:
        data = json.loads(replay_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"replay is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("replay root must be a JSON object")
    if "observations" not in data:
        raise ValueError("replay missing 'observations' array")
    return data


def parse_ts(value: Any) -> datetime | None:
    """Parse a Langfuse timestamp string into an aware UTC datetime.

    Args:
        value: Timestamp string (ISO 8601 with Z) or None.

    Returns:
        Aware datetime in UTC, or None when input is empty/malformed.

    Raises:
        None.

    Side Effects:
        None.
    """

    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (ValueError, AttributeError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_ms(value: Any) -> float | None:
    """Coerce a numeric millisecond value to float, tolerating None/str.

    Args:
        value: Latency/millisecond value from Langfuse, may be None or string.

    Returns:
        Float milliseconds or None when missing/invalid.

    Raises:
        None.

    Side Effects:
        None.
    """

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_get(dictionary: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Fetch a nested value from a dict, returning default on missing path.

    Args:
        dictionary: Source mapping.
        keys: Sequential keys forming the lookup path.
        default: Fallback value when any key is missing.

    Returns:
        Nested value or default.

    Raises:
        None.

    Side Effects:
        None.
    """

    current: Any = dictionary
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def cmd_summary(data: dict[str, Any]) -> int:
    """Print the replay summary block.

    Args:
        data: Loaded replay payload.

    Returns:
        Exit code 0.

    Raises:
        None.

    Side Effects:
        Writes a summary report to stdout.
    """

    observations = data["observations"]
    type_counter: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()
    level_counter: Counter[str] = Counter()
    total_latency_ms = 0.0
    latency_count = 0
    generation_tokens = 0
    error_count = 0

    for ob in observations:
        ob_type = ob.get("type", "unknown")
        type_counter[ob_type] += 1
        status = ob.get("status") or safe_get(ob, "output", "status") or "unknown"
        status_counter[str(status)] += 1
        level = ob.get("level")
        if level:
            level_counter[str(level)] += 1
        latency = to_ms(ob.get("latency")) or to_ms(safe_get(ob, "usage", "totalLatencyMs"))
        if latency is not None:
            total_latency_ms += latency
            latency_count += 1
        if ob_type == "GENERATION":
            input_tokens = to_ms(safe_get(ob, "usage", "inputTokens"))
            output_tokens = to_ms(safe_get(ob, "usage", "outputTokens"))
            if input_tokens is not None:
                generation_tokens += int(input_tokens)
            if output_tokens is not None:
                generation_tokens += int(output_tokens)
        if str(status).upper() == "ERROR":
            error_count += 1

    print("=== Replay Summary ===")
    print(f"trace_id       : {data.get('trace_id')}")
    print(f"source         : {data.get('source')}")
    print(f"fetched_at     : {data.get('fetched_at')}")
    print(f"base_url       : {data.get('base_url')}")
    print(f"fields         : {data.get('fields')}")
    print(f"page_count     : {data.get('page_count')}")
    print(f"observation_cnt: {data.get('observation_count')}")
    print(f"types          : {dict(type_counter)}")
    print(f"statuses       : {dict(status_counter)}")
    if level_counter:
        print(f"levels         : {dict(level_counter)}")
    if latency_count:
        avg = total_latency_ms / latency_count if latency_count else 0.0
        print(f"latency        : avg={avg:.1f}ms over {latency_count} observations")
    if generation_tokens:
        print(f"generation_tok : {generation_tokens}")
    if error_count:
        print(f"ERROR obs      : {error_count}")
    return 0


def _iso(dt: datetime | None) -> str:
    """Format a datetime as a compact UTC string for display.

    Args:
        dt: Aware datetime, or None.

    Returns:
        ISO-ish UTC string or '---' when None.

    Raises:
        None.

    Side Effects:
        None.
    """

    return dt.strftime("%H:%M:%S.%f")[:-3] if dt else "---"


def _fmt_duration(ms: float | None) -> str:
    """Format a millisecond duration compactly.

    Args:
        ms: Millisecond value or None.

    Returns:
        Human-readable duration string.

    Raises:
        None.

    Side Effects:
        None.
    """

    if ms is None:
        return "---"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"


def cmd_timeline(data: dict[str, Any]) -> int:
    """Print observations ordered by start time.

    Args:
        data: Loaded replay payload.

    Returns:
        Exit code 0.

    Raises:
        None.

    Side Effects:
        Writes the timeline to stdout.
    """

    observations = data["observations"]
    rows = []
    for ob in observations:
        start = parse_ts(ob.get("startTime") or ob.get("start_time"))
        end = parse_ts(ob.get("endTime") or ob.get("end_time"))
        latency = to_ms(ob.get("latency")) or to_ms(safe_get(ob, "usage", "totalLatencyMs"))
        if start and end:
            latency = (end - start).total_seconds() * 1000
        rows.append((start, ob, latency))

    rows.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc))

    print("=== Timeline (by start time) ===")
    for start, ob, latency in rows:
        ob_type = ob.get("type", "?")
        name = ob.get("name", "(unnamed)")
        status = ob.get("status") or safe_get(ob, "output", "status") or ""
        indent = "  " if ob.get("parentObservationId") else ""
        print(
            f"{indent}{_iso(start)} {ob_type:<10} {name:<28} "
            f"status={status:<8} {_fmt_duration(latency)}"
        )
    return 0


def cmd_hotspots(data: dict[str, Any]) -> int:
    """Print the slowest observations by latency.

    Args:
        data: Loaded replay payload.

    Returns:
        Exit code 0.

    Raises:
        None.

    Side Effects:
        Writes hotspots to stdout.
    """

    observations = data["observations"]
    measured = []
    for ob in observations:
        latency = to_ms(ob.get("latency")) or to_ms(safe_get(ob, "usage", "totalLatencyMs"))
        if latency is None:
            start = parse_ts(ob.get("startTime"))
            end = parse_ts(ob.get("endTime"))
            if start and end:
                latency = (end - start).total_seconds() * 1000
        if latency is not None:
            measured.append((latency, ob))

    measured.sort(key=lambda item: item[0], reverse=True)
    print("=== Hotspots (slowest observations) ===")
    for latency, ob in measured[:15]:
        name = ob.get("name", "(unnamed)")
        print(f"{_fmt_duration(latency):>10}  {ob.get('type', '?'):<10} {name}")
    return 0


def cmd_generations(data: dict[str, Any]) -> int:
    """Print generation observations with model/usage details.

    Args:
        data: Loaded replay payload.

    Returns:
        Exit code 0.

    Raises:
        None.

    Side Effects:
        Writes generation details to stdout.
    """

    observations = data["observations"]
    generations = [ob for ob in observations if ob.get("type") == "GENERATION"]
    print(f"=== Generations ({len(generations)}) ===")
    for ob in generations:
        name = ob.get("name", "(unnamed)")
        model = ob.get("model") or safe_get(ob, "modelParameters", "model") or "?"
        usage = ob.get("usage") or {}
        input_tokens = usage.get("inputTokens") or usage.get("promptTokens") or "?"
        output_tokens = usage.get("outputTokens") or usage.get("completionTokens") or "?"
        status = ob.get("status") or safe_get(ob, "output", "status") or ""
        print(f"{name:<28} model={model:<24} in={input_tokens} out={output_tokens} status={status}")
    return 0


def cmd_inspect(data: dict[str, Any], argv: list[str]) -> int:
    """Print compact JSON for a single observation id.

    Args:
        data: Loaded replay payload.
        argv: Extra CLI tokens; the first is treated as the observation id filter.

    Returns:
        Exit code 0.

    Raises:
        None.

    Side Effects:
        Writes the observation JSON to stdout.
    """

    target_id = argv[0] if argv else None
    observations = data["observations"]
    if target_id:
        matches = [ob for ob in observations if ob.get("id") == target_id]
        if not matches:
            print(f"no observation with id={target_id}", file=sys.stderr)
            return 1
        payload = matches[0]
    else:
        payload = observations[0] if observations else {}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_export_llm(data: dict[str, Any]) -> int:
    """Print a flattened, LLM-friendly trace for offline reasoning.

    Args:
        data: Loaded replay payload.

    Returns:
        Exit code 0.

    Raises:
        None.

    Side Effects:
        Writes the export payload to stdout.
    """

    observations = data["observations"]
    flat = []
    for ob in observations:
        start = parse_ts(ob.get("startTime") or ob.get("start_time"))
        latency = to_ms(ob.get("latency")) or to_ms(safe_get(ob, "usage", "totalLatencyMs"))
        if start and (parse_ts(ob.get("endTime")) or parse_ts(ob.get("end_time"))):
            latency = (
                (parse_ts(ob["endTime"]) - start).total_seconds() * 1000
                if ob.get("endTime")
                else None
            )
        node = {
            "id": ob.get("id"),
            "parent": ob.get("parentObservationId"),
            "type": ob.get("type"),
            "name": ob.get("name"),
            "start": start.isoformat() if start else None,
            "latency_ms": round(latency) if latency is not None else None,
            "status": ob.get("status") or safe_get(ob, "output", "status"),
            "model": ob.get("model"),
            "usage": ob.get("usage") or None,
            "level": ob.get("level"),
            "input_preview": _preview(safe_get(ob, "input", "body") if not ob.get("input") else ob.get("input")),
            "output_preview": _preview(
                safe_get(ob, "output", "body") if not ob.get("output") else ob.get("output")
            ),
        }
        flat.append(node)

    export = {
        "trace_id": data.get("trace_id"),
        "fetched_at": data.get("fetched_at"),
        "nodes": flat,
    }
    print(json.dumps(export, ensure_ascii=False, indent=2))
    return 0


def _preview(value: Any, limit: int = 240) -> str:
    """Produce a short string preview of arbitrary observation payload content.

    Args:
        value: Input/output body, which may be a string, dict, list, or None.
        limit: Maximum number of characters in the preview.

    Returns:
        Compact preview string.

    Raises:
        None.

    Side Effects:
        None.
    """

    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except TypeError:
            text = str(value)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the analysis tool.

    Args:
        None.

    Returns:
        Configured ArgumentParser.

    Raises:
        None.

    Side Effects:
        None.
    """

    parser = argparse.ArgumentParser(
        prog="analyze_langfuse_replay",
        description="Offline analysis for local Langfuse replay JSON files.",
    )
    parser.add_argument("replay", help="Path to replay JSON from fetch_langfuse_replay.py")
    parser.add_argument(
        "command",
        nargs="?",
        choices=STATUS_ORDER,
        default="summary",
        help="Analysis command (default: summary).",
    )
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="Command-specific extras.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for replay analysis.

    Args:
        argv: Optional argument vector; defaults to sys.argv[1:].

    Returns:
        Process exit code.

    Raises:
        None.

    Side Effects:
        Reads the replay file and writes reports to stdout.
    """

    args = build_parser().parse_args(argv)
    try:
        data = load_replay(Path(args.replay).expanduser())
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    handlers = {
        "summary": cmd_summary,
        "timeline": cmd_timeline,
        "hotspots": cmd_hotspots,
        "generations": cmd_generations,
        "inspect": cmd_inspect,
        "export-llm": cmd_export_llm,
    }
    return handlers[args.command](data, args.rest)


if __name__ == "__main__":
    raise SystemExit(main())
