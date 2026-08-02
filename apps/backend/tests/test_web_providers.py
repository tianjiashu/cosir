"""Web Provider adapter tests."""

import pytest

from app.tools.tool_handler.web.providers import default_web_providers
from app.tools.tool_handler.web.providers.firecrawl_provider import FirecrawlProvider
from app.tools.tool_handler.web.web_provider import (
    WebProviderUnavailableError,
    WebSearchItem,
)


def test_default_web_providers_follow_registry_priority() -> None:
    """验证默认 Provider 按注册表回退优先级返回。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 默认 Provider 名称顺序不符合约定时由断言抛出。

    副作用:
        创建一组默认 Provider 实例。
    """

    assert [provider.name for provider in default_web_providers()] == [
        "firecrawl",
    ]


def test_firecrawl_search_normalizes_results(monkeypatch) -> None:
    """验证 Firecrawl 搜索响应会归一化为当前 Provider 契约。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: 归一化结果与预期不一致时由断言抛出。

    副作用:
        临时替换 Firecrawl Provider 的 HTTP 客户端，避免真实网络请求。
    """

    provider = FirecrawlProvider(api_key="key")

    class FakeResponse:
        """提供固定 Firecrawl Search API 响应的测试替身。"""

        def raise_for_status(self) -> None:
            """模拟成功的 HTTP 状态检查。

            参数:
                无。

            返回:
                无。

            异常:
                无。

            副作用:
                无。
            """

        def json(self) -> dict[str, object]:
            """返回固定的 Firecrawl 搜索响应。

            参数:
                无。

            返回:
                含一条网页搜索结果的响应字典。

            异常:
                无。

            副作用:
                无。
            """
            return {
                "data": [
                    {
                        "title": "Title",
                        "url": "https://example.com",
                        "description": "Desc",
                    }
                ]
            }

    class FakeClient:
        """提供上下文管理接口的 HTTP 客户端测试替身。"""

        def __enter__(self) -> "FakeClient":
            """返回当前测试客户端。

            参数:
                无。

            返回:
                当前 ``FakeClient`` 实例。

            异常:
                无。

            副作用:
                无。
            """
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            """模拟上下文退出且不抑制异常。

            参数:
                exc_type: 当前异常类型或 ``None``。
                exc: 当前异常实例或 ``None``。
                tb: 当前异常回溯或 ``None``。

            返回:
                ``False``，表示不抑制异常。

            异常:
                无。

            副作用:
                无。
            """
            return False

        def post(self, url: str, json: dict[str, object], headers: dict[str, str]) -> FakeResponse:
            """返回固定的 HTTP 响应。

            参数:
                url: Provider 请求的目标地址。
                json: Provider 请求的 JSON 请求体。
                headers: Provider 请求的 HTTP 请求头。

            返回:
                固定的 ``FakeResponse`` 实例。

            异常:
                无。

            副作用:
                无。
            """
            return FakeResponse()

    monkeypatch.setattr(
        "app.tools.tool_handler.web.providers.firecrawl_provider.httpx.Client",
        lambda timeout: FakeClient(),
    )

    results = provider.search("query", 3)

    assert results == [
        WebSearchItem(
            title="Title",
            url="https://example.com",
            description="Desc",
            position=1,
        )
    ]


def test_firecrawl_rejects_unsupported_extract_format(monkeypatch) -> None:
    """验证 Firecrawl 在请求 API 前拒绝不支持的正文格式。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: 不支持格式未被拒绝或仍调用 Provider 请求入口时抛出。

    副作用:
        临时替换 Provider 请求入口，使意外请求立即导致测试失败。
    """

    provider = FirecrawlProvider(api_key="key")

    def fail_request(endpoint: str, body: dict[str, object]) -> object:
        """使意外的 Provider 请求立即失败。

        参数:
            endpoint: Provider 请求的相对端点。
            body: Provider 请求的 JSON 请求体。

        返回:
            不返回结果。

        异常:
            AssertionError: 总是抛出，表示不应发起 Provider 请求。

        副作用:
            无。
        """
        raise AssertionError(f"unexpected provider request: {endpoint} {body}")

    monkeypatch.setattr(provider, "_post", fail_request)

    with pytest.raises(WebProviderUnavailableError, match="does not support .* extraction format"):
        provider.extract(["https://example.com"], "text", 100)


def test_firecrawl_supports_search_and_extract() -> None:
    """验证 Firecrawl Provider 声明同时支持搜索与正文提取。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 能力声明与预期不符时由断言抛出。

    副作用:
        无。
    """

    provider = FirecrawlProvider(api_key="key")
    assert provider.supports_search() is True
    assert provider.supports_extract() is True
    assert provider.supported_extract_formats() == frozenset({"markdown", "html"})
