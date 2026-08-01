"""Web Provider adapter tests."""

import pytest

from app.tools.tool_handler.web.providers import default_web_providers
from app.tools.tool_handler.web.providers.brave_provider import BraveProvider
from app.tools.tool_handler.web.providers.exa_provider import ExaProvider
from app.tools.tool_handler.web.providers.firecrawl_provider import FirecrawlProvider
from app.tools.tool_handler.web.providers.parallel_provider import ParallelProvider
from app.tools.tool_handler.web.providers.searxng_provider import SearxngProvider
from app.tools.tool_handler.web.providers.tavily_provider import TavilyProvider
from app.tools.tool_handler.web.web_provider import (
    WebExtractItem,
    WebProviderUnavailableError,
    WebSearchItem,
)


def test_brave_normalizes_search_results(monkeypatch) -> None:
    """验证 Brave 搜索响应会被归一化为当前 Provider 契约。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: 归一化结果与预期不一致时由断言抛出。

    副作用:
        临时替换 Brave Provider 的 HTTP 客户端，避免真实网络请求。
    """

    provider = BraveProvider(api_key="key")

    class FakeResponse:
        """提供固定 Brave API 响应的测试替身。"""

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
            """返回固定的 Brave 搜索响应。

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
                "web": {
                    "results": [
                        {
                            "title": "Title",
                            "url": "https://example.com",
                            "description": "Desc",
                        }
                    ]
                }
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

        def get(self, url: str, params: dict[str, object], headers: dict[str, str]) -> FakeResponse:
            """返回固定的 HTTP 响应。

            参数:
                url: Provider 请求的目标地址。
                params: Provider 请求的查询参数。
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
        "app.tools.tool_handler.web.providers.brave_provider.httpx.Client",
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


def test_searxng_normalizes_search_results(monkeypatch) -> None:
    """验证 SearXNG 搜索响应会保留返回顺序并映射摘要。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: 归一化结果与预期不一致时由断言抛出。

    副作用:
        临时替换 SearXNG Provider 的 HTTP 客户端，避免真实网络请求。
    """

    provider = SearxngProvider(base_url="https://search.example")

    class FakeResponse:
        """提供固定 SearXNG API 响应的测试替身。"""

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
            """返回固定的 SearXNG 搜索响应。

            参数:
                无。

            返回:
                含两条搜索结果的响应字典。

            异常:
                无。

            副作用:
                无。
            """

            return {
                "results": [
                    {"title": "A", "url": "https://a.example", "content": "Alpha"},
                    {"title": "B", "url": "https://b.example", "content": "Beta"},
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

        def get(self, url: str, params: dict[str, str]) -> FakeResponse:
            """返回固定的 HTTP 响应。

            参数:
                url: Provider 请求的目标地址。
                params: Provider 请求的查询参数。

            返回:
                固定的 ``FakeResponse`` 实例。

            异常:
                无。

            副作用:
                无。
            """

            return FakeResponse()

    monkeypatch.setattr(
        "app.tools.tool_handler.web.providers.searxng_provider.httpx.Client",
        lambda timeout: FakeClient(),
    )

    results = provider.search("query", 2)

    assert results == [
        WebSearchItem(
            title="A",
            url="https://a.example",
            description="Alpha",
            position=1,
        ),
        WebSearchItem(
            title="B",
            url="https://b.example",
            description="Beta",
            position=2,
        ),
    ]


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
        "parallel",
        "tavily",
        "exa",
        "searxng",
        "brave-free",
        "ddgs",
    ]


def test_parallel_extract_normalizes_default_excerpts_from_v1(monkeypatch) -> None:
    """验证 Parallel V1 提取会保留默认 excerpts 作为正文内容。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: 请求端点或归一化结果不符合预期时由断言抛出。

    副作用:
        临时替换 Parallel Provider 的 HTTP 客户端，避免真实网络请求。
    """

    provider = ParallelProvider(api_key="key")
    requests: list[tuple[str, dict[str, object]]] = []

    class FakeResponse:
        """提供固定 Parallel Extract 响应的测试替身。"""

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
            """返回只包含默认 excerpts 的 Parallel V1 响应。

            参数:
                无。

            返回:
                含一条成功提取结果的响应字典。

            异常:
                无。

            副作用:
                无。
            """

            return {
                "results": [
                    {
                        "url": "https://example.com",
                        "title": "Example",
                        "excerpts": ["Alpha", "Beta"],
                        "publish_date": "2026-08-02",
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
                当前 FakeClient 实例。

            异常:
                无。

            副作用:
                无。
            """

            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            """模拟上下文退出且不抑制异常。

            参数:
                exc_type: 当前异常类型或 None。
                exc: 当前异常实例或 None。
                tb: 当前异常回溯或 None。

            返回:
                False，表示不抑制异常。

            异常:
                无。

            副作用:
                无。
            """

            return False

        def post(
            self,
            url: str,
            json: dict[str, object],
            headers: dict[str, str],
        ) -> FakeResponse:
            """记录请求并返回固定的 HTTP 响应。

            参数:
                url: Provider 请求的目标地址。
                json: Provider 请求的 JSON 请求体。
                headers: Provider 请求的 HTTP 请求头。

            返回:
                固定的 FakeResponse 实例。

            异常:
                无。

            副作用:
                将请求地址与请求体记录到当前测试列表。
            """

            requests.append((url, json))
            return FakeResponse()

    monkeypatch.setattr(
        "app.tools.tool_handler.web.providers.parallel_provider.httpx.Client",
        lambda timeout: FakeClient(),
    )

    results = provider.extract(["https://example.com"], "markdown", 100)

    assert requests == [
        (
            "https://api.parallel.ai/v1/extract",
            {"urls": ["https://example.com"]},
        )
    ]
    assert results == [
        WebExtractItem(
            url="https://example.com",
            title="Example",
            content="Alpha\nBeta",
            raw_content="Alpha\nBeta",
            metadata={"publish_date": "2026-08-02"},
        )
    ]


def test_parallel_search_normalizes_v1_excerpts(monkeypatch) -> None:
    """验证 Parallel V1 搜索会将 excerpts 合并为结果描述。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: 搜索结果描述未保留 V1 excerpts 时由断言抛出。

    副作用:
        临时替换 Parallel Provider 的 HTTP 请求方法，避免真实网络请求。
    """

    provider = ParallelProvider(api_key="key")
    monkeypatch.setattr(
        provider,
        "_post",
        lambda endpoint, body: {
            "results": [
                {
                    "title": "Example",
                    "url": "https://example.com",
                    "excerpts": ["Alpha", "Beta"],
                }
            ]
        },
    )

    assert provider.search("query", 1) == [
        WebSearchItem(
            title="Example",
            url="https://example.com",
            description="Alpha\nBeta",
            position=1,
        )
    ]


@pytest.mark.parametrize(
    ("provider", "output_format", "expected_request"),
    [
        (
            FirecrawlProvider(api_key="key"),
            "html",
            ("scrape", {"url": "https://example.com", "formats": ["html"]}),
        ),
        (
            TavilyProvider(api_key="key"),
            "text",
            ("extract", {"urls": ["https://example.com"], "format": "text"}),
        ),
        (
            ExaProvider(api_key="key"),
            "text",
            ("contents", {"urls": ["https://example.com"], "text": True}),
        ),
        (
            ParallelProvider(api_key="key"),
            "markdown",
            ("extract", {"urls": ["https://example.com"]}),
        ),
    ],
    ids=["firecrawl-html", "tavily-text", "exa-text", "parallel-markdown"],
)
def test_extract_supported_format_uses_provider_request_contract(
    monkeypatch,
    provider,
    output_format: str,
    expected_request: tuple[str, dict[str, object]],
) -> None:
    """验证支持的正文格式会映射到各 Provider 的实际请求契约。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。
        provider: 已配置的待测正文提取 Provider。
        output_format: 模型请求且 Provider 支持的正文格式。
        expected_request: Provider 应发起的端点与请求体。

    返回:
        无。

    异常:
        AssertionError: Provider 请求未遵守其实际 API 格式契约时抛出。

    副作用:
        临时替换 Provider 请求入口，避免真实网络请求。
    """

    requests: list[tuple[str, dict[str, object]]] = []

    def record_request(endpoint: str, body: dict[str, object]) -> object:
        """记录 Provider 请求并返回空结果。

        参数:
            endpoint: Provider 请求的相对端点。
            body: Provider 请求的 JSON 请求体。

        返回:
            不含提取结果的空响应字典。

        异常:
            无。

        副作用:
            向当前测试的请求记录列表追加一次调用。
        """

        requests.append((endpoint, body))
        return {}

    monkeypatch.setattr(provider, "_post", record_request)

    provider.extract(["https://example.com"], output_format, 100)

    assert requests == [expected_request]


@pytest.mark.parametrize(
    ("provider", "output_format"),
    [
        (FirecrawlProvider(api_key="key"), "text"),
        (TavilyProvider(api_key="key"), "html"),
        (ExaProvider(api_key="key"), "html"),
        (ParallelProvider(api_key="key"), "html"),
    ],
    ids=["firecrawl-text", "tavily-html", "exa-html", "parallel-html"],
)
def test_extract_rejects_unsupported_format_before_provider_request(
    monkeypatch,
    provider,
    output_format: str,
) -> None:
    """验证 Provider 在请求 API 前拒绝不支持的正文格式。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。
        provider: 已配置的待测正文提取 Provider。
        output_format: Provider 不支持的正文格式。

    返回:
        无。

    异常:
        AssertionError: 不支持格式未被拒绝或仍调用 Provider 请求入口时抛出。

    副作用:
        临时替换 Provider 请求入口，使意外请求立即导致测试失败。
    """

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
        provider.extract(["https://example.com"], output_format, 100)


def test_exa_search_handles_empty_highlights(monkeypatch) -> None:
    """验证 Exa 空 highlights 响应不会导致搜索归一化崩溃。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: 搜索结果未以空描述返回时由断言抛出。

    副作用:
        临时替换 Exa Provider 的 HTTP 请求方法，避免真实网络请求。
    """

    provider = ExaProvider(api_key="key")
    monkeypatch.setattr(
        provider,
        "_post",
        lambda endpoint, body: {
            "results": [
                {
                    "title": "Example",
                    "url": "https://example.com",
                    "highlights": [],
                }
            ]
        },
    )

    assert provider.search("query", 1) == [
        WebSearchItem(
            title="Example",
            url="https://example.com",
            description="",
            position=1,
        )
    ]
