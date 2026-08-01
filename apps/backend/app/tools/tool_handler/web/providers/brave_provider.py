"""Brave Search API provider."""

import os

import httpx

from app.config.settings import Settings
from app.tools.tool_handler.web.web_provider import (
    WebExtractItem,
    WebProviderUnavailableError,
    WebSearchItem,
)


class BraveProvider:
    """通过 Brave Search API 提供网页搜索。"""

    name = "brave-free"

    def __init__(self, api_key: str | None = None) -> None:
        """初始化 Brave Provider。

        参数:
            api_key: 可选的 Brave API Key；省略时从环境变量读取。

        返回:
            无。

        异常:
            无。

        副作用:
            读取 BRAVE_SEARCH_API_KEY 环境变量。
        """

        self._api_key = (
            api_key if api_key is not None else os.environ.get("BRAVE_SEARCH_API_KEY", "")
        )

    @property
    def display_name(self) -> str:
        """返回 Brave 的可读名称。

        参数:
            无。

        返回:
            Provider 的可读名称。

        异常:
            无。

        副作用:
            无。
        """

        return "Brave Search"

    def is_available(self) -> bool:
        """判断 Brave API Key 是否存在。

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
        """返回 Brave 的搜索能力状态。

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
        """返回 Brave 的正文提取能力状态。

        参数:
            无。

        返回:
            始终返回 False。

        异常:
            无。

        副作用:
            无。
        """

        return False

    def missing_configuration_message(self) -> str:
        """返回 Brave 未配置时的英文诊断信息。

        参数:
            无。

        返回:
            简洁英文配置说明。

        异常:
            无。

        副作用:
            无。
        """

        return "Brave Search is unavailable: set BRAVE_SEARCH_API_KEY."

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """使用 Brave Search API 执行网页搜索。

        参数:
            query: 搜索关键词。
            limit: 最多返回的搜索结果数量。

        返回:
            归一化后的网页搜索结果。

        异常:
            WebProviderUnavailableError: 未配置 Brave API Key 时抛出。
            httpx.HTTPError: Brave API 请求失败时抛出。

        副作用:
            发起 Brave Search API 网络请求。
        """

        if not self.is_available():
            raise WebProviderUnavailableError(self.missing_configuration_message())
        with httpx.Client(timeout=Settings.WEB_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": limit},
                headers={"Accept": "application/json", "X-Subscription-Token": self._api_key},
            )
            response.raise_for_status()
        payload: object = response.json()
        web = payload.get("web") if isinstance(payload, dict) else None
        raw_results = web.get("results") if isinstance(web, dict) else None
        if not isinstance(raw_results, list):
            return []
        return [
            WebSearchItem(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("description", "")),
            )
            for item in raw_results[:limit]
            if isinstance(item, dict) and item.get("url")
        ]

    def extract(self, urls: list[str], char_limit: int) -> list[WebExtractItem]:
        """拒绝 Brave 不支持的正文提取调用。

        参数:
            urls: 待提取的网页地址列表。
            char_limit: 单页正文最大字符数。

        返回:
            不返回结果。

        异常:
            WebProviderUnavailableError: 始终抛出，说明 Brave 不支持正文提取。

        副作用:
            无。
        """

        raise WebProviderUnavailableError("Brave Search does not support web extraction.")
