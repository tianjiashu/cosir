"""网页工具的 UI 展示数据构造。"""

from collections.abc import Iterable, Mapping
from typing import Any


def build_web_search_display_data(
    *, query: str, results: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """构造网页搜索展示数据。

    UI 只消费 query、标题和 URL；provider、摘要和排名留在模型结果或后端诊断通道。
    """

    projected = []
    for item in results:
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        title = item.get("title")
        projected.append({"title": title if isinstance(title, str) else "", "url": url})
    return {"kind": "web-search-results", "query": query, "results": projected}


def build_web_extract_display_data(
    *, urls: Iterable[str], partial: bool = False
) -> dict[str, Any]:
    """构造网页正文提取的 URL 列表。

    UI 不消费 provider、正文、metadata 或逐站点错误；网站名称和图标由前端从 URL 处理。
    """

    data: dict[str, Any] = {
        "kind": "web-extract-urls",
        "urls": [{"url": url} for url in urls if isinstance(url, str) and url],
    }
    if partial:
        data["status_hint"] = "部分成功"
    return data
