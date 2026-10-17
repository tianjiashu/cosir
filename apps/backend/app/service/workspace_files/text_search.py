"""全文查找使用分段解码；跨段保留查询长度的重叠区，不把整个大文件装入内存。"""

import codecs
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

from app.service.workspace_files.file_access import regular_file
from app.service.workspace_files.file_content import read_text


class TextMatch(TypedDict):
    offset: int
    snippet: str


class TextSearchBatch(TypedDict):
    matches: list[TextMatch]
    complete: bool


def search_text(
    path: Path, query: str, encoding: str, version: str, cancelled: threading.Event, *, root: Path
) -> Iterator[TextSearchBatch]:
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    effective_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
    with regular_file(path, root) as source:
        head = source.read(4)
    bom_size = next(
        (
            len(marker)
            for marker in (
                codecs.BOM_UTF32_LE,
                codecs.BOM_UTF32_BE,
                codecs.BOM_UTF8,
                codecs.BOM_UTF16_LE,
                codecs.BOM_UTF16_BE,
            )
            if head.startswith(marker)
        ),
        0,
    )
    offset = 0
    overlap = ""
    overlap_offset = 0
    while not cancelled.is_set():
        page = read_text(path, offset, encoding, version, root=root, cancelled=cancelled)
        text = overlap + page.text
        base_offset = overlap_offset if overlap else offset + (bom_size if offset == 0 else 0)
        matches: list[TextMatch] = []
        byte_offset, character_offset = base_offset, 0
        for match in pattern.finditer(text):
            if match.end() <= len(overlap):
                continue
            byte_offset += len(text[character_offset : match.start()].encode(effective_encoding))
            character_offset = match.start()
            matches.append(
                {
                    "offset": byte_offset,
                    "snippet": text[max(0, match.start() - 40) : match.end() + 100],
                }
            )
            if len(matches) >= 100:
                yield {"matches": matches, "complete": False}
                matches = []
                if cancelled.is_set():
                    return
        yield {"matches": matches, "complete": page.next_offset is None}
        if page.next_offset is None:
            return
        overlap = text[-(len(query) - 1) :] if len(query) > 1 else ""
        overlap_offset = page.next_offset - len(overlap.encode(effective_encoding))
        offset = page.next_offset
