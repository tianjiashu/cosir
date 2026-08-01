"""Firecrawl API provider."""

import os

import httpx

from app.config.settings import Settings
from app.tools.tool_handler.web.web_provider import (
    WebExtractItem,
    WebProviderUnavailableError,
    WebSearchItem,
)

_DEFAULT_BASE_URL = "https://api.firecrawl.dev/v1"


class FirecrawlProvider:
    """通过 Firecrawl API 提供网页搜索与正文提取。"""

    name = "firecrawl"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        """初始化 Firecrawl Provider。

        参数:
            api_key: 可选的 Firecrawl API Key；省略时从环境变量读取。
            base_url: 可选的 Firecrawl API 根地址；省略时从环境变量读取。

        返回:
            无。

        异常:
            无。

        副作用:
            读取 FIRECRAWL_API_KEY 与 FIRECRAWL_API_URL 环境变量。
        """

        self._api_key = api_key if api_key is not None else os.environ.get("FIRECRAWL_API_KEY", "")
        self._base_url = (
            base_url if base_url is not None else os.environ.get("FIRECRAWL_API_URL", "")
        )

    @property
    def display_name(self) -> str:
        """返回 Firecrawl 的可读名称。

        参数:
            无。

        返回:
            Provider 的可读名称。

        异常:
            无。

        副作用:
            无。
        """

        return "Firecrawl"

    def is_available(self) -> bool:
        """判断 Firecrawl 所需 API Key 或自定义地址是否存在。

        参数:
            无。

        返回:
            配置 API Key 或 API 根地址时返回 True。

        异常:
            无。

        副作用:
            无。
        """

        return bool(self._api_key or self._base_url)

    def supports_search(self) -> bool:
        """返回 Firecrawl 的搜索能力状态。

        参数:
            无。

        返回:
            始终返回 True。

        异常:
            无。

        副作用:
            无。
        """

        return True

    def supports_extract(self) -> bool:
        """返回 Firecrawl 的正文提取能力状态。

        参数:
            无。

        返回:
            始终返回 True。

        异常:
            无。

        副作用:
            无。
        """

        return True

    def missing_configuration_message(self) -> str:
        """返回 Firecrawl 未配置时的英文诊断信息。

        参数:
            无。

        返回:
            简洁英文配置说明。

        异常:
            无。

        副作用:
            无。
        """

        return "Firecrawl is unavailable: set FIRECRAWL_API_KEY or FIRECRAWL_API_URL."

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """使用 Firecrawl API 执行网页搜索。

        参数:
            query: 搜索关键词。
            limit: 最多返回的搜索结果数量。

        返回:
            归一化后的网页搜索结果。

        异常:
            WebProviderUnavailableError: 缺少 Firecrawl 配置时抛出。
            httpx.HTTPError: Firecrawl API 请求失败时抛出。

        副作用:
            发起 Firecrawl Search API 网络请求。
        """

        payload = self._post("search", {"query": query, "limit": limit})
        raw_results = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list):
            return []
        return [
            WebSearchItem(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("description") or item.get("markdown") or ""),
            )
            for item in raw_results[:limit]
            if isinstance(item, dict) and item.get("url")
        ]

    def extract(self, urls: list[str], char_limit: int) -> list[WebExtractItem]:
        """使用 Firecrawl Scrape API 逐页提取网页正文。

        参数:
            urls: 待提取的网页地址列表。
            char_limit: 每页正文最大保留字符数。

        返回:
            归一化并截断到字符上限的正文结果。

        异常:
            WebProviderUnavailableError: 缺少 Firecrawl 配置时抛出。
            httpx.HTTPError: Firecrawl API 请求失败时抛出。

        副作用:
            为每个 URL 发起一次 Firecrawl Scrape API 网络请求。
        """

        results: list[WebExtractItem] = []
        for url in urls:
            payload = self._post("scrape", {"url": url, "formats": ["markdown"]})
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                continue
            metadata = data.get("metadata")
            title = metadata.get("title", "") if isinstance(metadata, dict) else ""
            results.append(
                WebExtractItem(
                    url=str(data.get("url") or url),
                    title=str(title),
                    content=str(data.get("markdown") or data.get("content") or "")[:char_limit],
                )
            )
        return results

    def _post(self, endpoint: str, body: dict[str, object]) -> object:
        """向 Firecrawl API 发送 JSON 请求。

        参数:
            endpoint: Firecrawl API 相对端点。
            body: JSON 请求体。

        返回:
            Firecrawl 返回的 JSON 响应。

        异常:
            WebProviderUnavailableError: 缺少 Firecrawl 配置时抛出。
            httpx.HTTPError: HTTP 请求失败时抛出。

        副作用:
            发起 Firecrawl API 网络请求。
        """

        if not self.is_available():
            raise WebProviderUnavailableError(self.missing_configuration_message())
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        base_url = self._base_url.rstrip("/") or _DEFAULT_BASE_URL
        with httpx.Client(timeout=Settings.WEB_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post(f"{base_url}/{endpoint}", json=body, headers=headers)
            response.raise_for_status()
        return response.json()
