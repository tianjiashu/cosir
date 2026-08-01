"""Exa API provider."""

import os

import httpx

from app.config.settings import Settings
from app.tools.tool_handler.web.web_provider import (
    WebExtractItem,
    WebProviderUnavailableError,
    WebSearchItem,
    provider_result_metadata,
)


class ExaProvider:
    """通过 Exa API 提供网页搜索与正文提取。"""

    name = "exa"

    def __init__(self, api_key: str | None = None) -> None:
        """初始化 Exa Provider。

        参数:
            api_key: 可选的 Exa API Key；省略时从环境变量读取。

        返回:
            无。

        异常:
            无。

        副作用:
            读取 EXA_API_KEY 环境变量。
        """

        self._api_key = api_key if api_key is not None else os.environ.get("EXA_API_KEY", "")

    @property
    def display_name(self) -> str:
        """返回 Exa 的可读名称。

        参数:
            无。

        返回:
            Provider 的可读名称。

        异常:
            无。

        副作用:
            无。
        """

        return "Exa"

    def is_available(self) -> bool:
        """判断 Exa API Key 是否存在。

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
        """返回 Exa 的搜索能力状态。

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
        """返回 Exa 的正文提取能力状态。

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
        """返回 Exa 未配置时的英文诊断信息。

        参数:
            无。

        返回:
            简洁英文配置说明。

        异常:
            无。

        副作用:
            无。
        """

        return "Exa is unavailable: set EXA_API_KEY."

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """使用 Exa API 执行网页搜索。

        参数:
            query: 搜索关键词。
            limit: 最多返回的搜索结果数量。

        返回:
            归一化后的网页搜索结果。

        异常:
            WebProviderUnavailableError: 未配置 Exa API Key 时抛出。
            httpx.HTTPError: Exa API 请求失败时抛出。

        副作用:
            发起 Exa Search API 网络请求。
        """

        payload = self._post(
            "search",
            {"query": query, "numResults": limit, "contents": {"text": True}},
        )
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list):
            return []
        return [
            WebSearchItem(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                description=self._description(item),
                position=position,
            )
            for position, item in enumerate(raw_results[:limit], start=1)
            if isinstance(item, dict) and item.get("url")
        ]

    def extract(
        self,
        urls: list[str],
        output_format: str,
        char_limit: int,
    ) -> list[WebExtractItem]:
        """使用 Exa API 提取网页正文。

        参数:
            urls: 待提取的网页地址列表。
            output_format: 调用方请求的网页正文格式。
            char_limit: 每页正文最大保留字符数。

        返回:
            归一化并截断到字符上限的正文结果。

        异常:
            WebProviderUnavailableError: 未配置 Exa API Key 时抛出。
            httpx.HTTPError: Exa API 请求失败时抛出。

        副作用:
            发起 Exa Contents API 网络请求。
        """

        payload = self._post(
            "contents",
            {"urls": urls, "text": True, "output_format": output_format},
        )
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(raw_results, list):
            return []
        return [
            WebExtractItem(
                url=str(item.get("url", "")),
                title=str(item.get("title", "")),
                content=str(item.get("text") or "")[:char_limit],
                raw_content=str(item.get("text") or ""),
                metadata=provider_result_metadata(
                    item,
                    frozenset({"url", "title", "text", "error"}),
                ),
                error=str(item.get("error") or ""),
            )
            for item in raw_results
            if isinstance(item, dict) and item.get("url")
        ]

    def _post(self, endpoint: str, body: dict[str, object]) -> object:
        """向 Exa API 发送认证后的 JSON 请求。

        参数:
            endpoint: Exa API 相对端点。
            body: JSON 请求体。

        返回:
            Exa 返回的 JSON 响应。

        异常:
            WebProviderUnavailableError: 未配置 API Key 时抛出。
            httpx.HTTPError: HTTP 请求失败时抛出。

        副作用:
            发起 Exa API 网络请求。
        """

        if not self.is_available():
            raise WebProviderUnavailableError(self.missing_configuration_message())
        with httpx.Client(timeout=Settings.WEB_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"https://api.exa.ai/{endpoint}",
                json=body,
                headers={"x-api-key": self._api_key},
            )
            response.raise_for_status()
        return response.json()

    def _description(self, item: dict[str, object]) -> str:
        """从 Exa 搜索结果中选择稳定的描述文本。

        参数:
            item: Exa 返回的单条搜索结果。

        返回:
            优先返回 text；没有 text 时返回首个 highlights；两者都不可用时返回空字符串。

        异常:
            无。

        副作用:
            无。
        """

        text = item.get("text")
        if text:
            return str(text)
        highlights = item.get("highlights")
        if isinstance(highlights, list) and highlights:
            return str(highlights[0])
        return ""
