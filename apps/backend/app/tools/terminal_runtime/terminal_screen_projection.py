"""将 worker 屏幕投影为有界模型文本；裁剪旧行，保留底部最新内容。"""

import json
from typing import Any


def project_screen(source: dict[str, Any] | None, budget: int) -> dict[str, Any] | None:
    """坐标保持原屏幕零基值；裁剪用起始行与行内省略字符数明确标记。"""
    if source is None:
        return None
    rows = [line["text"] for line in source["lines"]]
    while rows and not rows[-1]:
        rows.pop()
    screen = {
        "text": "\n".join(rows),
        "row_offset": 0,
        "first_row_omitted_chars": 0,
        "wrapped_rows": [
            i for i, line in enumerate(source["lines"][: len(rows)]) if line["wrapped"]
        ],
        "cursor": source["cursor"].copy(),
        "alternate": source["alternate"],
        "truncated": False,
    }

    def size() -> int:
        return len(json.dumps(screen, ensure_ascii=False, separators=(",", ":")))

    while rows and size() > budget:
        screen["truncated"] = True
        if len(rows) > 1:
            rows.pop(0)
            screen["row_offset"] += 1
            screen["wrapped_rows"] = [
                i for i in screen["wrapped_rows"] if i >= screen["row_offset"]
            ]
        else:
            # 单行含大量组合字符时保留后缀；坐标不再由裁剪后的字符数反推。
            remove = max(1, len(rows[0]) // 2)
            rows[0] = rows[0][remove:]
            screen["first_row_omitted_chars"] += remove
            if not rows[0]:
                rows.clear()
        screen["text"] = "\n".join(rows)
    return screen
