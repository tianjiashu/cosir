"""Web search and extraction provider contracts."""

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class WebSearchItem:
    """Web search result returned by a provider."""

    title: str
    url: str
    description: str
    position: int


@dataclass(frozen=True, slots=True)
class WebExtractItem:
    """Web page content extracted by a provider.

    ``content`` 已由 Provider 按调用方下发的 ``char_limit`` 截断，``truncated``
    标记原文是否被裁剪，使模型能区分「页面就这么短」与「正文被截断」。
    """

    url: str
    title: str
    content: str
    metadata: dict[str, object]
    truncated: bool = False
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

    # 语义：本地配置缺失，重试无意义，调用方应返回 retryable=False 的观察。


class WebProviderRequestError(RuntimeError):
    """Raised when a provider is configured but the remote call fails."""

    # 语义：配置正确但远端返回业务失败（HTTP 200 + success:false 等），
    # 属瞬态故障，调用方应返回 retryable=True 的观察，与配置缺失区分开。


def unsupported_extract_format_message(provider_name: str, output_format: str) -> str:
    """构造 Provider 不支持正文格式时的稳定英文错误文本。

    参数:
        provider_name: Provider 的可读显示名称。
        output_format: 调用方请求但 Provider 不支持的正文格式。

    返回:
        面向模型和用户的简洁英文格式不支持说明。

    异常:
        无。

    副作用:
        无。
    """

    return f"{provider_name} does not support '{output_format}' extraction format."


def ensure_supported_extract_format(
    provider_name: str,
    output_format: str,
    supported_formats: frozenset[str],
) -> None:
    """在 Provider API 调用前校验正文格式能力。

    参数:
        provider_name: Provider 的可读显示名称。
        output_format: 调用方请求的网页正文格式。
        supported_formats: Provider 实际支持的正文格式集合。

    返回:
        无。

    异常:
        WebProviderUnavailableError: 请求格式不在 Provider 支持集合中时抛出。

    副作用:
        无；只执行本地能力校验，不发起网络请求。
    """

    if output_format not in supported_formats:
        raise WebProviderUnavailableError(
            unsupported_extract_format_message(provider_name, output_format)
        )


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

    def supported_extract_formats(self) -> frozenset[str]:
        """返回 Provider 实际支持的网页正文格式集合。

        参数:
            无。

        返回:
            支持正文提取时返回可请求格式的不可变集合；不支持提取时返回空集合。

        异常:
            无。

        副作用:
            无；只读取 Provider 的静态能力声明，不发起网络请求。
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

    def extract(
        self,
        urls: list[str],
        output_format: Literal["markdown", "html"],
        char_limit: int,
    ) -> list[WebExtractItem] | Awaitable[list[WebExtractItem]]:
        """提取网页正文内容。

        参数:
            urls: 待提取的网页地址列表。
            output_format: 调用方请求的网页正文格式（仅 markdown / html）。
            char_limit: 每个网页最多保留的正文字符数；Provider 必须据此截断
                ``content`` 并置 ``truncated``。

        返回:
            与 ``urls`` 顺序一致的结果列表；单页失败不中断其余页面，该页结果带
            ``error`` 字段（由上层判定「全部失败 / 部分失败」）。

        异常:
            WebProviderUnavailableError: Provider 未配置或不可用时抛出。

        副作用:
            可能发起网络请求。
        """
