"""验证 model-node chunk 分析文档中的统计数字是否可复现。

读取仓库根 `logs/debug_merged_chunks.jsonl` 与 `logs/debug_raw_chunks.jsonl`，
输出文档「第四节 / 第七节」引用的关键统计：MERGED 按 run 分组、finish_reason 分布、
content 非空率、最大并行工具调用、RAW 的 invalid_tool_calls 次数。

用法（Windows cmd）：
    uv run python scripts/verify_chunk_stats.py

用法（Git Bash / WSL）：
    python scripts/verify_chunk_stats.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


def _load(path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _run_prefix(rid: str | None) -> str:
    if not rid:
        return "unknown"
    return rid.replace("lc_run--", "").split("-")[0]


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    merged_path = root / "logs" / "debug_merged_chunks.jsonl"
    raw_path = root / "logs" / "debug_raw_chunks.jsonl"

    merged = _load(merged_path)
    raw = _load(raw_path)

    # MERGED 按 run 分组
    groups: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "tc": 0, "stop": 0, "c": 0, "mx": 0}
    )
    for i, row in enumerate(merged):
        m = row["merged"]
        p = _run_prefix(m.get("id"))
        fr = (m.get("response_metadata") or {}).get("finish_reason")
        g = groups[p]
        g["n"] += 1
        g["mx"] = max(g["mx"], len(m.get("tool_calls") or []))
        if fr == "stop":
            g["stop"] += 1
        else:
            g["tc"] += 1
        if m.get("content"):
            g["c"] += 1
        print(f"idx={i:2d} run={p} finish={fr} content={'Y' if m.get('content') else 'N'} "
              f"len={len(m.get('content') or '')}")

    print("\n=== MERGED 分组汇总 ===")
    tot_tc = tot_stop = tot_c = 0
    max_all = 0
    for p, g in groups.items():
        print(f"  run={p} steps={g['n']} tool_calls={g['tc']} stop={g['stop']} "
              f"content非空={g['c']}/{g['n']} max_toolcalls={g['mx']}")
        tot_tc += g["tc"]
        tot_stop += g["stop"]
        tot_c += g["c"]
        max_all = max(max_all, g["mx"])
    print(f"  >>> 汇总: steps={sum(g['n'] for g in groups.values())} "
          f"tool_calls={tot_tc} stop={tot_stop} content非空={tot_c}/32 max并行={max_all}")

    # RAW invalid_tool_calls 统计
    itc_total = 0
    for row in raw:
        itc = row["chunk"].get("invalid_tool_calls") or []
        if itc:
            itc_total += len(itc)
    print(f"\nRAW invalid_tool_calls 总条数: {itc_total}  (RAW 总行数: {len(raw)})")


if __name__ == "__main__":
    main()
