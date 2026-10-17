"""流式搜索的结果窗口：限制单页内存，仍可继续查看全部匹配。"""

import threading
from collections.abc import Iterator
from dataclasses import asdict

from app.service.workspace_files.file_models import SearchBatch
from app.service.workspace_files.text_search import TextMatch, TextSearchBatch


def file_search_pages(
    batches: Iterator[SearchBatch], offset: int, limit: int, cancelled: threading.Event
) -> Iterator[dict[str, object]]:
    seen = 0
    delivered = 0
    for batch in batches:
        selected: list[dict[str, object]] = []
        for entry in batch.entries:
            if seen >= offset:
                if delivered == limit:
                    yield {
                        "entries": selected,
                        "skipped": batch.skipped,
                        "skipped_reasons": batch.skipped_reasons,
                        "complete": True,
                        "next_offset": offset + limit,
                    }
                    cancelled.set()
                    return
                selected.append(asdict(entry))
                delivered += 1
            seen += 1
        yield {
            "entries": selected,
            "skipped": batch.skipped,
            "skipped_reasons": batch.skipped_reasons,
            "complete": batch.complete,
            "next_offset": None,
        }


def text_search_pages(
    batches: Iterator[TextSearchBatch], offset: int, limit: int, cancelled: threading.Event
) -> Iterator[dict[str, object]]:
    """全文匹配同样有界；翻页重查磁盘并验证版本，不保留隐形长期搜索任务。"""
    seen = 0
    delivered = 0
    for batch in batches:
        selected: list[TextMatch] = []
        for match in batch["matches"]:
            if seen >= offset:
                if delivered == limit:
                    yield {"matches": selected, "complete": True, "next_offset": offset + limit}
                    cancelled.set()
                    return
                selected.append(match)
                delivered += 1
            seen += 1
        yield {"matches": selected, "complete": batch["complete"], "next_offset": None}
