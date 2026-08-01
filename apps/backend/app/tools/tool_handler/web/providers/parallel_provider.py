"""Parallel API provider."""

import os

import httpx

from app.config.settings import Settings
from app.tools.tool_handler.web.web_provider import (
    WebExtractItem,
    WebProviderUnavailableError,
    WebSearchItem,
    ensure_supported_extract_format,
    provider_result_metadata,
)


class ParallelProvider:
    """通过 Parallel API 提供网页搜索与正文提取。"""

    name = "parallel"

    def __init__(self, api_key: str | None = None) -> None:
        """初始化 Parallel Provider。

        参数:
            api_key: 可选的 Parallel API Key；省略时从环境变量读取。

        返回:
            无。

        异常:
            无。

        副作用:
            读取 PARALLEL_API_KEY 环境变量。
        """

        self._api_key = api_key if api_key is not None else os.environ.get("PARALLEL_API_KEY", "")

    @property
    def display_name(self) -> str:
        """返回 Parallel 的可读名称。

        参数:
            无。

        返回:
            Provider 的可读名称。

        异常:
            无。

        副作用:
            无。
        """

        return "Parallel"

    def is_available(self) -> bool:
        """判断 Parallel API Key 是否存在。

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
        """返回 Parallel 的搜索能力状态。

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
        """返回 Parallel 的正文提取能力状态。

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

    def supported_extract_formats(self) -> frozenset[str]:
        """返回 Parallel Extract API 实际支持的正文格式。

        参数:
            无。

        返回:
            只包含 API 固定返回的 Markdown 格式集合。

        异常:
            无。

        副作用:
            无。
        """

        return frozenset({"markdown"})

    def missing_configuration_message(self) -> str:
        """返回 Parallel 未配置时的英文诊断信息。

        参数:
            无。

        返回:
            简洁英文配置说明。

        异常:
            无。

        副作用:
            无。
        """

        return "Parallel is unavailable: set PARALLEL_API_KEY."

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """使用 Parallel API 执行网页搜索。

        参数:
            query: 搜索关键词。
            limit: 最多返回的搜索结果数量。

        返回:
            归一化后的网页搜索结果。

        异常:
            WebProviderUnavailableError: 未配置 Parallel API Key 时抛出。
            httpx.HTTPError: Parallel API 请求失败时抛出。

        副作用:
            发起 Parallel Search API 网络请求。
        """

        payload = self._post(
            "search",
            {"search_queries": [query], "max_results": limit},
        )
        raw_results = self._results(payload)
        return [
            WebSearchItem(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                description=self._content(item),
                position=position,
            )
            for position, item in enumerate(raw_results[:limit], start=1)
            if item.get("url")
        ]

    def extract(
        self,
        urls: list[str],
        output_format: str,
        char_limit: int,
    ) -> list[WebExtractItem]:
        """使用 Parallel API 提取网页正文。

        参数:
            urls: 待提取的网页地址列表。
            output_format: 调用方请求的网页正文格式。
            char_limit: 每页正文最大保留字符数。

        返回:
            归一化并截断到字符上限的正文结果。

        异常:
            WebProviderUnavailableError: 未配置 Parallel API Key 时抛出。
            httpx.HTTPError: Parallel API 请求失败时抛出。

        副作用:
            发起 Parallel Extract API 网络请求。
        """

        ensure_supported_extract_format(
            self.display_name,
            output_format,
            self.supported_extract_formats(),
        )
        payload = self._post("extract", {"urls": urls})
        return [
            WebExtractItem(
                url=str(item.get("url", "")),
                title=str(item.get("title", "")),
                content=self._content(item)[:char_limit],
                raw_content=self._content(item),
                metadata=provider_result_metadata(
                    item,
                    frozenset({"url", "title", "content", "full_content", "excerpts", "error"}),
                ),
                error=str(item.get("error") or ""),
            )
            for item in self._results(payload)
            if item.get("url")
        ]

    def _post(self, endpoint: str, body: dict[str, object]) -> object:
        """向 Parallel API 发送认证后的 JSON 请求。

        参数:
            endpoint: Parallel API 相对端点。
            body: JSON 请求体。

        返回:
            Parallel 返回的 JSON 响应。

        异常:
            WebProviderUnavailableError: 未配置 API Key 时抛出。
            httpx.HTTPError: HTTP 请求失败时抛出。

        副作用:
            发起 Parallel API 网络请求。
        """

        if not self.is_available():
            raise WebProviderUnavailableError(self.missing_configuration_message())
        with httpx.Client(timeout=Settings.WEB_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"https://api.parallel.ai/v1/{endpoint}",
                json=body,
                headers={"x-api-key": self._api_key},
            )
            response.raise_for_status()
        return response.json()

    def _results(self, payload: object) -> list[dict[str, object]]:
        """从 Parallel 响应中读取扁平或嵌套结果列表。

        参数:
            payload: Parallel 返回的 JSON 响应。

        返回:
            可归一化的结果字典列表。

        异常:
            无。

        副作用:
            无。
        """

        if not isinstance(payload, dict):
            return []
        raw_results = payload.get("results") or payload.get("data")
        if not isinstance(raw_results, list):
            return []
        if len(raw_results) == 1 and isinstance(raw_results[0], dict):
            nested_results = raw_results[0].get("results")
            if isinstance(nested_results, list):
                raw_results = nested_results
        return [item for item in raw_results if isinstance(item, dict)]

    def _content(self, item: dict[str, object]) -> str:
        """从 Parallel 提取结果中选择完整正文或默认摘要。

        参数:
            item: Parallel 返回的单条提取结果。

        返回:
            优先返回完整正文；缺失时返回由 excerpts 拼接的正文；都不存在时返回空字符串。

        异常:
            无。

        副作用:
            无。
        """

        content = item.get("content") or item.get("full_content")
        if content:
            return str(content)
        excerpts = item.get("excerpts")
        if isinstance(excerpts, list):
            return "\n".join(str(excerpt) for excerpt in excerpts)
        return ""
