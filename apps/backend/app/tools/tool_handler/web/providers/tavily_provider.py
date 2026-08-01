"""Tavily API provider."""

import os

import httpx

from app.config.settings import Settings
from app.tools.tool_handler.web.web_provider import (
    WebExtractItem,
    WebProviderUnavailableError,
    WebSearchItem,
)


class TavilyProvider:
    """通过 Tavily API 提供网页搜索与正文提取。"""

    name = "tavily"

    def __init__(self, api_key: str | None = None) -> None:
        """初始化 Tavily Provider。

        参数:
            api_key: 可选的 Tavily API Key；省略时从环境变量读取。

        返回:
            无。

        异常:
            无。

        副作用:
            读取 TAVILY_API_KEY 环境变量。
        """

        self._api_key = api_key if api_key is not None else os.environ.get("TAVILY_API_KEY", "")

    @property
    def display_name(self) -> str:
        """返回 Tavily 的可读名称。

        参数:
            无。

        返回:
            Provider 的可读名称。

        异常:
            无。

        副作用:
            无。
        """

        return "Tavily"

    def is_available(self) -> bool:
        """判断 Tavily API Key 是否存在。

        参数:
            无。

        返回:
            已配置 API Key 时返回 True。

        异常:
            无。

        副作用:
            无。
        """

        return bool(self._api_key)

    def supports_search(self) -> bool:
        """返回 Tavily 的搜索能力状态。

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
        """返回 Tavily 的正文提取能力状态。

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
        """返回 Tavily 未配置时的英文诊断信息。

        参数:
            无。

        返回:
            简洁英文配置说明。

        异常:
            无。

        副作用:
            无。
        """

        return "Tavily is unavailable: set TAVILY_API_KEY."

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """使用 Tavily API 执行网页搜索。

        参数:
            query: 搜索关键词。
            limit: 最多返回的搜索结果数量。

        返回:
            归一化后的网页搜索结果。

        异常:
            WebProviderUnavailableError: 未配置 Tavily API Key 时抛出。
            httpx.HTTPError: Tavily API 请求失败时抛出。

        副作用:
            发起 Tavily Search API 网络请求。
        """

        payload = self._post("search", {"query": query, "max_results": limit})
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list):
            return []
        return [
            WebSearchItem(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("content", "")),
            )
            for item in raw_results[:limit]
            if isinstance(item, dict) and item.get("url")
        ]

    def extract(self, urls: list[str], char_limit: int) -> list[WebExtractItem]:
        """使用 Tavily API 提取网页正文。

        参数:
            urls: 待提取的网页地址列表。
            char_limit: 每页正文最大保留字符数。

        返回:
            归一化并截断到字符上限的正文结果。

        异常:
            WebProviderUnavailableError: 未配置 Tavily API Key 时抛出。
            httpx.HTTPError: Tavily API 请求失败时抛出。

        副作用:
            发起 Tavily Extract API 网络请求。
        """

        payload = self._post("extract", {"urls": urls})
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list):
            return []
        return [
            WebExtractItem(
                url=str(item.get("url", "")),
                title=str(item.get("title", "")),
                content=str(item.get("raw_content") or item.get("content") or "")[:char_limit],
            )
            for item in raw_results
            if isinstance(item, dict) and item.get("url")
        ]

    def _post(self, endpoint: str, body: dict[str, object]) -> object:
        """向 Tavily API 发送认证后的 JSON 请求。

        参数:
            endpoint: Tavily API 相对端点。
            body: 不含认证信息的 JSON 请求体。

        返回:
            Tavily 返回的 JSON 响应。

        异常:
            WebProviderUnavailableError: 未配置 API Key 时抛出。
            httpx.HTTPError: HTTP 请求失败时抛出。

        副作用:
            发起 Tavily API 网络请求。
        """

        if not self.is_available():
            raise WebProviderUnavailableError(self.missing_configuration_message())
        with httpx.Client(timeout=Settings.WEB_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"https://api.tavily.com/{endpoint}",
                json={"api_key": self._api_key, **body},
            )
            response.raise_for_status()
        return response.json()
