"""缩略框守卫：后端交付物缩略框是前端显示尺寸的两倍（两倍清晰度），两边各写一份数字，改一边漏一边就在这里失败。

前端显示尺寸：方块是 ``lib/image/tile-size.ts`` 的 ``IMAGE_TILE_SIZE.regular``，单张大图卡是
``lib/assistant/turn/deliverable-layout.ts`` 的 ``IMAGE_CARD_MAX``。框小了缩略图发糊，大了白白多传字节。
"""

import re
from pathlib import Path

from app.service.workspace_files.image_preview import _PREVIEW_BOXES

_DESKTOP = Path("../desktop/lib")


def _size(file: str, pattern: str) -> tuple[int, int]:
    source = (_DESKTOP / file).read_text(encoding="utf-8")
    match = re.search(pattern + r"\s*\{\s*width:\s*(\d+),\s*height:\s*(\d+)\s*\}", source)
    assert match, f"{file} 里尺寸的写法变了，守卫需要同步"
    return int(match.group(1)), int(match.group(2))


def test_preview_boxes_are_twice_frontend_sizes() -> None:
    tile = _size("image/tile-size.ts", r"regular:")
    card = _size("assistant/turn/deliverable-layout.ts", r"IMAGE_CARD_MAX =")
    assert _PREVIEW_BOXES["tile"] == (tile[0] * 2, tile[1] * 2)
    assert _PREVIEW_BOXES["card"] == (card[0] * 2, card[1] * 2)
