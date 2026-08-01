"""Web search and extraction provider contracts."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WebSearchItem:
    """Web search result returned by a provider."""

    title: str
    url: str
    description: str
    position: int


@dataclass(frozen=True, slots=True)
class WebExtractItem:
    """Web page content extracted by a provider."""

    url: str
    title: str
    content: str
    raw_content: str
    metadata: dict[str, object]
    error: str = ""


def provider_result_metadata(
    item: dict[str, object],
    excluded_fields: frozenset[str],
) -> dict[str, object]:
    """提取 Provider 结果中的结构化元数据。

    参数:
        item: Provider 返回的单条原始结果。
        excluded_fields: 已映射到 WebExtractItem 专有字段的键名。

    返回:
        Provider 明确返回 metadata 时使用其副本，否则返回未映射字段。

    异常:
        无。

    副作用:
        无。
    """

    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    return {key: value for key, value in item.items() if key not in excluded_fields}


class WebProviderUnavailableError(RuntimeError):
    """Raised when a selected web provider lacks required configuration."""


class WebProvider(Protocol):
    """Contract implemented by web search and extraction provider adapters."""

    name: str

    @property
    def display_name(self) -> str:
        """返回供用户界面和诊断展示的 Provider 名称。

        参数:
            无。

        返回:
            可读的 Provider 显示名称。

        异常:
            无。

        副作用:
            无。
        """

    def is_available(self) -> bool:
        """判断 Provider 是否已具备本地调用所需配置。

        参数:
            无。

        返回:
            已配置且可调用时返回 ``True``，否则返回 ``False``。

        异常:
            无。

        副作用:
            仅读取本地配置，不发起网络请求。
        """

    def supports_search(self) -> bool:
        """判断 Provider 是否支持网页搜索能力。

        参数:
            无。

        返回:
            支持搜索时返回 ``True``，否则返回 ``False``。

        异常:
            无。

        副作用:
            无。
        """

    def supports_extract(self) -> bool:
        """判断 Provider 是否支持网页正文提取能力。

        参数:
            无。

        返回:
            支持正文提取时返回 ``True``，否则返回 ``False``。

        异常:
            无。

        副作用:
            无。
        """

    def missing_configuration_message(self) -> str:
        """返回 Provider 缺少配置时的英文诊断信息。

        参数:
            无。

        返回:
            面向模型和用户的简洁英文配置缺失说明。

        异常:
            无。

        副作用:
            无。
        """

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """执行网页搜索。

        参数:
            query: 搜索关键词。
            limit: 最多返回的结果数量。

        返回:
            按相关性排序的搜索结果。

        异常:
            WebProviderUnavailableError: Provider 未配置或不可用时抛出。

        副作用:
            可能发起网络请求。
        """

    def extract(self, urls: list[str], char_limit: int) -> list[WebExtractItem]:
        """提取网页正文内容。

        参数:
            urls: 待提取的网页地址列表。
            char_limit: 每个网页最多保留的正文字符数。

        返回:
            与成功提取网页对应的正文结果。

        异常:
            WebProviderUnavailableError: Provider 未配置或不可用时抛出。

        副作用:
            可能发起网络请求。
        """
