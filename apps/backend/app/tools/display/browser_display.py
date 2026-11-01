"""内置浏览器工具的 UI 展示数据构造（纯函数，不执行 IO）。"""

from __future__ import annotations

from typing import Any


def build_browser_page_display_data(
    page: dict[str, Any],
    *,
    screenshot: str | None = None,
    handoff_reason: str | None = None,
) -> dict[str, Any]:
    """浏览器工具的页面卡片：标签、地址与标题，可带截图（附件 asset id）或交接说明。

    快照正文只给模型，不进展示数据（体积大，且 UI 用不上）；卡片据 ``tab_id`` 在工作台打开实时页面。
    """

    data: dict[str, Any] = {
        "kind": "browser-page",
        "tab_id": page["tab_id"],
        "url": page["url"],
        "title": page["title"],
    }
    if screenshot is not None:
        data["screenshot"] = screenshot
    if handoff_reason is not None:
        data["handoff_reason"] = handoff_reason
    return data
