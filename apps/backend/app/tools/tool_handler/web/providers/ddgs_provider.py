"""DuckDuckGo search provider backed by the optional ddgs package."""

import importlib

from app.tools.tool_handler.web.web_provider import (
    WebExtractItem,
    WebProviderUnavailableError,
    WebSearchItem,
)


class DdgsProvider:
    """通过可选 ddgs 包提供无需 API Key 的网页搜索。"""

    name = "ddgs"

    @property
    def display_name(self) -> str:
        """返回 DDGS 的可读名称。

        参数:
            无。

        返回:
            Provider 的可读名称。

        异常:
            无。

        副作用:
            无。
        """

        return "DuckDuckGo Search"

    def is_available(self) -> bool:
        """判断 ddgs 包是否可被当前 Python 环境导入。

        参数:
            无。

        返回:
            可导入 ddgs 包时返回 True。

        异常:
            无。导入失败时返回 False。

        副作用:
            尝试导入本地 ddgs Python 包，不发起网络请求。
        """

        try:
            importlib.import_module("ddgs")
        except ImportError:
            return False
        return True

    def supports_search(self) -> bool:
        """返回 DDGS 的搜索能力状态。

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
        """返回 DDGS 的正文提取能力状态。

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

    def supported_extract_formats(self) -> frozenset[str]:
        """返回 DDGS 支持的正文格式集合。

        参数:
            无。

        返回:
            空集合，因为 DDGS 不支持正文提取。

        异常:
            无。

        副作用:
            无。
        """

        return frozenset()

    def missing_configuration_message(self) -> str:
        """返回 ddgs 包不可用时的英文诊断信息。

        参数:
            无。

        返回:
            简洁英文依赖缺失说明。

        异常:
            无。

        副作用:
            无。
        """

        return "DuckDuckGo Search is unavailable: install the ddgs package."

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """使用 ddgs 包执行网页搜索。

        参数:
            query: 搜索关键词。
            limit: 最多返回的搜索结果数量。

        返回:
            归一化后的网页搜索结果。

        异常:
            WebProviderUnavailableError: ddgs 包不可用时抛出。
            RuntimeError: ddgs 搜索执行失败时由底层库抛出。

        副作用:
            通过 ddgs 包发起网页搜索请求。
        """

        if not self.is_available():
            raise WebProviderUnavailableError(self.missing_configuration_message())
        from ddgs import DDGS

        raw_results = DDGS().text(query, max_results=limit)
        return [
            WebSearchItem(
                title=str(item.get("title", "")),
                url=str(item.get("href", "")),
                description=str(item.get("body", "")),
                position=position,
            )
            for position, item in enumerate(raw_results, start=1)
            if isinstance(item, dict) and item.get("href")
        ]

    def extract(self, urls: list[str], output_format: str, char_limit: int) -> list[WebExtractItem]:
        """拒绝 DDGS 不支持的正文提取调用。

        参数:
            urls: 待提取的网页地址列表。
            output_format: 调用方请求的网页正文格式。
            char_limit: 单页正文最大字符数。

        返回:
            不返回结果。

        异常:
            WebProviderUnavailableError: 始终抛出，说明 DDGS 不支持正文提取。

        副作用:
            无。
        """

        raise WebProviderUnavailableError("DuckDuckGo Search does not support web extraction.")
