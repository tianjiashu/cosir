"""网页工具的卡片展示数据：卡片只列出处，摘要、正文与逐页错误只给模型。网站名称与图标由前端从 URL 取。"""

from typing import Any

from app.core.tool.handler.web.firecrawl_client import WebFetchItem, WebSearchItem


def build_web_search_display_data(*, query: str, results: list[WebSearchItem]) -> dict[str, Any]:
    return {
        "kind": "web-search-results",
        "query": query,
        "results": [{"title": item.title, "url": item.url} for item in results],
    }


def build_web_fetch_display_data(items: list[WebFetchItem]) -> dict[str, Any]:
    return {
        "kind": "web-fetch-urls",
        "partial": any(item.error for item in items),
        "urls": [{"url": item.url} for item in items],
    }
