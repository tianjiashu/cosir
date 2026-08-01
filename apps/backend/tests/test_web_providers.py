"""Web Provider adapter tests."""

from app.tools.tool_handler.web.providers import default_web_providers
from app.tools.tool_handler.web.providers.brave_provider import BraveProvider
from app.tools.tool_handler.web.providers.searxng_provider import SearxngProvider
from app.tools.tool_handler.web.web_provider import WebSearchItem


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

    assert results == [WebSearchItem(title="Title", url="https://example.com", snippet="Desc")]


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

    assert [(item.title, item.url, item.snippet) for item in results] == [
        ("A", "https://a.example", "Alpha"),
        ("B", "https://b.example", "Beta"),
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
