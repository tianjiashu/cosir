#!/usr/bin/env python3
"""离线分析 Langfuse replay JSON 的 CLI。

单一职责：读取 ``fetch_langfuse_replay.py`` 已持久化的本地 JSON，生成适合开发者和
Agent 排查问题使用的摘要、时间线、热点、模型调用清单、单 observation 详情与 LLM 报告。
脚本不联网、不读取密钥，也不导入 ``app.*``。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_DEFAULT_LIMIT = 20
_DEFAULT_MAX_CHARS = 30000


class ReplayAnalysisError(RuntimeError):
    """Langfuse replay 分析失败。"""


def load_replay(path: Path) -> dict[str, Any]:
    """读取并校验本地 replay JSON。

    参数:
        path: replay JSON 文件路径。
    返回:
        解析后的 replay 字典。
    异常:
        ReplayAnalysisError: 如果文件不存在、JSON 非法或基础结构不符合预期。
    副作用:
        读取本地文件。
    """

    if not path.exists():
        raise ReplayAnalysisError(f"replay file not found: {path}")
    try:
        replay = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReplayAnalysisError(f"invalid replay JSON: {path}") from exc
    if not isinstance(replay, dict):
        raise ReplayAnalysisError("replay root must be a JSON object")
    observations = replay.get("observations")
    if not isinstance(observations, list):
        raise ReplayAnalysisError("replay.observations must be a list")
    return replay


def observations_of(replay: dict[str, Any]) -> list[dict[str, Any]]:
    """返回 replay 中的 observation 字典列表。

    参数:
        replay: 已加载的 replay 字典。
    返回:
        observation 字典列表，非字典元素会被忽略。
    异常:
        无。
    副作用:
        无。
    """

    return [item for item in replay.get("observations", []) if isinstance(item, dict)]


def build_summary(replay: dict[str, Any]) -> dict[str, Any]:
    """构建 trace 摘要。

    参数:
        replay: 已加载的 replay 字典。
    返回:
        包含数量、类型分布、耗时、usage 和 root 信息的摘要字典。
    异常:
        无。
    副作用:
        无。
    """

    observations = observations_of(replay)
    type_counts = Counter(str(item.get("type") or "UNKNOWN") for item in observations)
    level_counts = Counter(str(item.get("level") or "UNKNOWN") for item in observations)
    name_counts = Counter(str(item.get("name") or "") for item in observations)
    latencies = [_float_or_zero(item.get("latency")) for item in observations]
    generations = [item for item in observations if item.get("type") == "GENERATION"]
    total_usage = sum(_int_or_zero(item.get("totalUsage")) for item in observations)
    input_usage = sum(_int_or_zero(item.get("inputUsage")) for item in observations)
    output_usage = sum(_int_or_zero(item.get("outputUsage")) for item in observations)
    sum_observation_latency = round(sum(latencies), 3)
    trace_wall_time = round(max(latencies) if latencies else 0.0, 3)
    tree = replay.get("tree") if isinstance(replay.get("tree"), dict) else {}
    return {
        "trace_id": str(replay.get("trace_id") or ""),
        "source": str(replay.get("source") or ""),
        "fetched_at": str(replay.get("fetched_at") or ""),
        "observation_count": len(observations),
        "type_counts": dict(type_counts),
        "level_counts": dict(level_counts),
        "logical_roots": list(tree.get("logical_roots") or []),
        "orphan_ids": list(tree.get("orphan_ids") or []),
        "generation_count": len(generations),
        "total_usage": total_usage,
        "input_usage": input_usage,
        "output_usage": output_usage,
        "sum_observation_latency": sum_observation_latency,
        "trace_wall_time": trace_wall_time,
        "total_latency": sum_observation_latency,
        "max_latency": trace_wall_time,
        "top_names": dict(name_counts.most_common(10)),
    }


def build_timeline(
    replay: dict[str, Any],
    *,
    observation_type: str = "",
    name_contains: str = "",
    contains: str = "",
    errors_only: bool = False,
) -> list[dict[str, Any]]:
    """构建按时间排序的压缩 observation 时间线。

    参数:
        replay: 已加载的 replay 字典。
        observation_type: 可选 observation type 精确过滤。
        name_contains: 可选 name 片段过滤。
        contains: 可选全文片段过滤。
        errors_only: 是否只返回 ERROR 级别 observation。
    返回:
        时间线条目列表。
    异常:
        无。
    副作用:
        无。
    """

    rows = []
    for item in sorted(observations_of(replay), key=_observation_sort_key):
        if not _matches_observation(item, observation_type, name_contains, contains, errors_only):
            continue
        rows.append(
            {
                "id": str(item.get("id") or ""),
                "parent": str(item.get("parentObservationId") or ""),
                "start_time": str(item.get("startTime") or ""),
                "end_time": str(item.get("endTime") or ""),
                "latency": _float_or_zero(item.get("latency")),
                "type": str(item.get("type") or ""),
                "name": str(item.get("name") or ""),
                "level": str(item.get("level") or ""),
                "status_message": str(item.get("statusMessage") or ""),
            }
        )
    return rows


def build_hotspots(replay: dict[str, Any], *, limit: int = _DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """构建按 latency 倒序排列的耗时热点列表。

    参数:
        replay: 已加载的 replay 字典。
        limit: 最大返回数量。
    返回:
        热点 observation 列表。
    异常:
        无。
    副作用:
        无。
    """

    if limit < 0:
        raise ValueError("limit must be greater than or equal to 0")
    timeline = build_timeline(replay)
    return sorted(timeline, key=lambda item: item["latency"], reverse=True)[:limit]


def build_generations(replay: dict[str, Any]) -> list[dict[str, Any]]:
    """构建模型调用 observation 摘要。

    参数:
        replay: 已加载的 replay 字典。
    返回:
        generation observation 摘要列表。
    异常:
        无。
    副作用:
        无。
    """

    generations = []
    for item in build_timeline(replay, observation_type="GENERATION"):
        original = inspect_observation(replay, item["id"])
        generations.append(
            {
                **item,
                "model": str(original.get("model") or ""),
                "input_usage": _int_or_zero(original.get("inputUsage")),
                "output_usage": _int_or_zero(original.get("outputUsage")),
                "total_usage": _int_or_zero(original.get("totalUsage")),
                "time_to_first_token": _float_or_zero(original.get("timeToFirstToken")),
                "usage_details": original.get("usageDetails") or {},
            }
        )
    return generations


def inspect_observation(replay: dict[str, Any], observation_id: str) -> dict[str, Any]:
    """按 id 查找单个 observation。

    参数:
        replay: 已加载的 replay 字典。
        observation_id: observation id。
    返回:
        匹配的 observation 字典。
    异常:
        ReplayAnalysisError: 如果找不到指定 observation。
    副作用:
        无。
    """

    for item in observations_of(replay):
        if str(item.get("id") or "") == observation_id:
            return item
    raise ReplayAnalysisError(f"observation not found: {observation_id}")


def build_llm_report(replay: dict[str, Any], *, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
    """构建适合 LLM 分析的 Markdown 报告。

    参数:
        replay: 已加载的 replay 字典。
        max_chars: 报告最大字符数。
    返回:
        Markdown 报告文本。
    异常:
        ValueError: 如果 max_chars 小于 1000。
    副作用:
        无。
    """

    if max_chars < 1000:
        raise ValueError("max_chars must be at least 1000")
    summary = build_summary(replay)
    hotspots = build_hotspots(replay, limit=10)
    generations = build_generations(replay)
    timeline = build_timeline(replay)
    sections = [
        "# Langfuse Replay Analysis",
        "\n".join(("## Summary", _json_block(summary))),
        "\n".join(("## Latency Hotspots", _json_block(hotspots))),
        "\n".join(("## Generations", _json_block(generations))),
        "\n".join(("## Timeline", _json_block(timeline))),
    ]
    return _join_report_sections(sections, max_chars=max_chars)


def write_text_file(output_path: Path, content: str, *, force: bool = False) -> Path:
    """写入文本文件，默认拒绝覆盖。

    参数:
        output_path: 输出文件路径。
        content: 待写入文本。
        force: 是否允许覆盖已有文件。
    返回:
        实际写入路径。
    异常:
        FileExistsError: 如果文件已存在且未启用 force。
        OSError: 如果目录创建或写入失败。
    副作用:
        创建父目录并写入文件。
    """

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and not force:
        raise FileExistsError(f"output file already exists: {output_path}")
    output_path.write_text(content, encoding="utf-8")
    return output_path


def render_text(value: Any) -> str:
    """把分析结果渲染为人读文本。

    参数:
        value: 分析结果，通常是字典或列表。
    返回:
        多行文本。
    异常:
        无。
    副作用:
        无。
    """

    if isinstance(value, dict):
        return "\n".join(f"{key}: {value[key]}" for key in value)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(_render_dict_row(item))
            else:
                lines.append(str(item))
        return "\n".join(lines)
    return str(value)


def _join_report_sections(sections: list[str], *, max_chars: int) -> str:
    """按 section 粒度拼接 Markdown 报告并执行字符预算。

    参数:
        sections: 已渲染的 Markdown section 列表。
        max_chars: 报告最大字符数。

    返回:
        不超过预算的 Markdown 文本；预算不足时只省略完整 section。

    异常:
        无。

    副作用:
        无。
    """

    suffix = "\n\n[TRUNCATED: omitted sections exceeded max_chars]"
    selected: list[str] = []
    for section in sections:
        candidate = "\n\n".join([*selected, section])
        if len(candidate) <= max_chars:
            selected.append(section)
            continue
        truncated = "\n\n".join([*selected, suffix.strip()])
        if len(truncated) <= max_chars:
            return truncated
        return suffix.strip()[:max_chars]
    return "\n\n".join(selected)


def _render_dict_row(item: dict[str, Any]) -> str:
    """把单行字典分析结果渲染为包含排查关键信息的文本。

    参数:
        item: 单行分析结果。

    返回:
        一行人读文本。

    异常:
        无。

    副作用:
        无。
    """

    parts = [
        str(item.get("start_time", "")),
        f"latency={item.get('latency', '')}",
        str(item.get("type", "")),
        str(item.get("name", "")),
        str(item.get("level", "")),
        str(item.get("id", "")),
    ]
    if item.get("model"):
        parts.append(f"model={item['model']}")
    if "total_usage" in item:
        parts.append(f"usage={item['total_usage']}")
    if "time_to_first_token" in item:
        parts.append(f"ttft={item['time_to_first_token']}")
    return " ".join(str(part) for part in parts if str(part)).strip()


def _validate_output_path(replay_path: Path, output_path: Path) -> None:
    """校验输出路径不会覆盖输入 replay。

    参数:
        replay_path: 输入 replay 路径。
        output_path: 输出文件路径。

    返回:
        无。

    异常:
        ValueError: 如果输出路径与输入 replay 路径相同。

    副作用:
        解析本地路径。
    """

    if replay_path.resolve() == output_path.resolve():
        raise ValueError("refusing to overwrite replay input file")


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
        prog="analyze_langfuse_replay",
        description="Analyze a local Langfuse replay JSON file offline.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_common_output_options(
        subparsers.add_parser("summary", help="Show trace summary."),
        default_format="text",
    )
    timeline = _add_common_output_options(
        subparsers.add_parser("timeline", help="Show compact observation timeline."),
        default_format="text",
    )
    timeline.add_argument("--type", default="", help="Filter by observation type.")
    timeline.add_argument("--name-contains", default="", help="Filter by name text.")
    timeline.add_argument("--contains", default="", help="Search observation fields.")
    timeline.add_argument("--errors-only", action="store_true", help="Only ERROR observations.")
    hotspots = _add_common_output_options(
        subparsers.add_parser("hotspots", help="Show slowest observations."),
        default_format="text",
    )
    hotspots.add_argument("--limit", type=int, default=10, help="Maximum rows.")
    _add_common_output_options(
        subparsers.add_parser("generations", help="Show model generation observations."),
        default_format="text",
    )
    inspect_parser = _add_common_output_options(
        subparsers.add_parser("inspect", help="Show one observation."),
        default_format="json",
    )
    inspect_parser.add_argument("observation_id", help="Observation id.")
    export = _add_common_output_options(
        subparsers.add_parser("export-llm", help="Export an LLM-oriented Markdown report."),
        default_format="markdown",
    )
    export.add_argument("--max-chars", type=int, default=_DEFAULT_MAX_CHARS, help="Budget.")
    return parser


def _add_common_output_options(
    parser: argparse.ArgumentParser,
    *,
    default_format: str,
) -> argparse.ArgumentParser:
    """为子命令追加公共输入输出参数。

    参数:
        parser: 子命令解析器。
        default_format: 默认输出格式。
    返回:
        原解析器，便于继续追加子命令专属参数。
    异常:
        无。
    副作用:
        修改解析器参数定义。
    """

    parser.add_argument("replay", help="Local replay JSON path.")
    parser.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default=default_format,
        help="Output format.",
    )
    parser.add_argument("--output", default="", help="Optional output file.")
    parser.add_argument("--force", action="store_true", help="Overwrite output file.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    参数:
        argv: 可选命令行参数列表；省略时使用 ``sys.argv[1:]``。
    返回:
        进程退出码，0 表示成功，1 表示用户错误或分析失败。
    异常:
        无。
    副作用:
        读取 replay JSON，向 stdout/stderr 输出，可选写文件。
    """

    args = build_parser().parse_args(argv)
    try:
        replay = load_replay(Path(args.replay).expanduser())
        result = _dispatch(args, replay)
        rendered = _render_output(result, args.format)
        if args.output.strip():
            output_path = Path(args.output).expanduser()
            _validate_output_path(Path(args.replay).expanduser(), output_path)
            write_text_file(output_path, rendered + "\n", force=args.force)
    except (ReplayAnalysisError, ValueError, OSError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(rendered)
    return 0


def _dispatch(args: argparse.Namespace, replay: dict[str, Any]) -> Any:
    """根据子命令执行对应分析。

    参数:
        args: argparse 解析后的命名空间。
        replay: 已加载的 replay 字典。
    返回:
        子命令分析结果。
    异常:
        ReplayAnalysisError: 如果子命令未知或观察对象不存在。
        ValueError: 如果参数非法。
    副作用:
        无。
    """

    if args.command == "summary":
        return build_summary(replay)
    if args.command == "timeline":
        return build_timeline(
            replay,
            observation_type=args.type.strip(),
            name_contains=args.name_contains.strip(),
            contains=args.contains.strip(),
            errors_only=args.errors_only,
        )
    if args.command == "hotspots":
        return build_hotspots(replay, limit=args.limit)
    if args.command == "generations":
        return build_generations(replay)
    if args.command == "inspect":
        return inspect_observation(replay, args.observation_id)
    if args.command == "export-llm":
        return build_llm_report(replay, max_chars=args.max_chars)
    raise ReplayAnalysisError(f"unknown command: {args.command}")


def _render_output(result: Any, output_format: str) -> str:
    """按指定格式渲染分析结果。

    参数:
        result: 分析结果。
        output_format: ``text``、``json`` 或 ``markdown``。
    返回:
        渲染后的文本。
    异常:
        ValueError: 如果格式非法。
    副作用:
        无。
    """

    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)
    if output_format == "markdown":
        if isinstance(result, str):
            return result
        return "```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```"
    if output_format == "text":
        return render_text(result)
    raise ValueError("format must be text, json, or markdown")


def _matches_observation(
    observation: dict[str, Any],
    observation_type: str,
    name_contains: str,
    contains: str,
    errors_only: bool,
) -> bool:
    """判断 observation 是否匹配时间线过滤条件。

    参数:
        observation: 单个 observation 字典。
        observation_type: 类型过滤条件。
        name_contains: 名称片段过滤条件。
        contains: 全文字面片段过滤条件。
        errors_only: 是否只看 ERROR 级别。
    返回:
        是否匹配。
    异常:
        无。
    副作用:
        无。
    """

    if observation_type and str(observation.get("type") or "") != observation_type:
        return False
    if errors_only and str(observation.get("level") or "") != "ERROR":
        return False
    if name_contains and name_contains not in str(observation.get("name") or ""):
        return False
    if contains:
        haystack = json.dumps(observation, ensure_ascii=False, default=str)
        if contains not in haystack:
            return False
    return True


def _observation_sort_key(observation: dict[str, Any]) -> tuple[str, str]:
    """返回稳定时间排序键。

    参数:
        observation: 单个 observation 字典。
    返回:
        ``(startTime, id)`` 元组。
    异常:
        无。
    副作用:
        无。
    """

    return str(observation.get("startTime") or ""), str(observation.get("id") or "")


def _json_block(value: Any) -> str:
    """渲染 Markdown JSON 代码块。

    参数:
        value: 任意 JSON 可序列化值。
    返回:
        Markdown 代码块文本。
    异常:
        TypeError: 如果值不可 JSON 序列化。
    副作用:
        无。
    """

    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n```"


def _float_or_zero(value: Any) -> float:
    """把输入转换为 float，失败时返回 0。

    参数:
        value: 任意输入。
    返回:
        浮点数或 0。
    异常:
        无。
    副作用:
        无。
    """

    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int_or_zero(value: Any) -> int:
    """把输入转换为 int，失败时返回 0。

    参数:
        value: 任意输入。
    返回:
        整数或 0。
    异常:
        无。
    副作用:
        无。
    """

    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
