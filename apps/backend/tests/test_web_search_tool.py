"""web_search 工具行为测试。"""

import json

from app.config.settings import Settings
from app.tools.tool_handler.web.web_provider import WebSearchItem
from app.tools.tool_handler.web.web_provider_registry import WebProviderRegistry
from app.tools.tool_handler.web.web_search import WebSearchTool


class FakeSearchProvider:
    """提供固定搜索结果的本地 Web Provider 测试替身。"""

    name = "fake"
    display_name = "Fake"

    def __init__(self, fail_on: str = "") -> None:
        """初始化测试替身的调用记录。

        参数:
            fail_on: 需要抛出异常的本地 Provider 方法名；空字符串表示不抛出。

        返回:
            无。

        异常:
            无。

        副作用:
            创建用于断言的搜索调用记录。
        """

        self._fail_on = fail_on
        self.search_limits: list[int] = []

    def is_available(self) -> bool:
        """返回 Provider 的本地可用状态。

        参数:
            无。

        返回:
            始终返回 ``True``。

        异常:
            无。

        副作用:
            无。
        """

        if self._fail_on == "is_available":
            raise RuntimeError("availability check failed")
        return True

    def supports_search(self) -> bool:
        """返回 Provider 的搜索能力。

        参数:
            无。

        返回:
            始终返回 ``True``。

        异常:
            无。

        副作用:
            无。
        """

        if self._fail_on == "supports_search":
            raise RuntimeError("capability check failed")
        return True

    def supports_extract(self) -> bool:
        """返回 Provider 的提取能力。

        参数:
            无。

        返回:
            始终返回 ``False``。

        异常:
            无。

        副作用:
            无。
        """

        return False

    def supported_extract_formats(self) -> frozenset[str]:
        """返回测试搜索 Provider 支持的正文格式集合。

        参数:
            无。

        返回:
            空集合，因为该测试替身不支持正文提取。

        异常:
            无。

        副作用:
            无。
        """

        return frozenset()

    def missing_configuration_message(self) -> str:
        """返回未配置时的诊断文本。

        参数:
            无。

        返回:
            固定英文诊断文本。

        异常:
            无。

        副作用:
            无。
        """

        return "Fake is not configured"

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """记录调用并返回固定的搜索元数据。

        参数:
            query: 搜索关键词。
            limit: 搜索结果上限。

        返回:
            含一条固定搜索结果的列表。

        异常:
            无。

        副作用:
            记录收到的结果上限。
        """

        self.search_limits.append(limit)
        return [WebSearchItem("Title", "https://example.com", "Desc", 1)]


def test_web_search_returns_metadata_only(monkeypatch) -> None:
    """验证搜索成功时仅将元数据返回给模型。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: 返回载荷包含正文或字段不符合契约时抛出。

    副作用:
        无（仅调用本地测试替身）。
    """

    registry = WebProviderRegistry()
    registry.register(FakeSearchProvider())
    tool = WebSearchTool(provider_registry=registry)
    monkeypatch.setattr(Settings, "WEB_SEARCH_BACKEND", "fake")

    observation = tool.execute("python", limit=1)
    payload = json.loads(observation.content)

    assert observation.status == "success"
    assert payload["success"] is True
    assert payload["data"]["web"][0] == {
        "title": "Title",
        "url": "https://example.com",
        "description": "Desc",
        "position": 1,
        "provider": "fake",
    }
    assert "content" not in payload["data"]["web"][0]


def test_web_search_reports_missing_provider() -> None:
    """验证未配置搜索 Provider 时返回可操作的工具错误。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 缺失 Provider 未返回预期错误时抛出。

    副作用:
        无。
    """

    tool = WebSearchTool(provider_registry=WebProviderRegistry())

    observation = tool.execute("python", limit=1)

    assert observation.status == "error"
    assert "No web search provider configured" in observation.error


def test_web_search_clamps_limit_before_calling_provider(monkeypatch) -> None:
    """验证搜索结果上限不会超过全局配置。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: Provider 收到未钳制的上限时抛出。

    副作用:
        无（仅调用本地测试替身）。
    """

    provider = FakeSearchProvider()
    registry = WebProviderRegistry()
    registry.register(provider)
    tool = WebSearchTool(provider_registry=registry)
    monkeypatch.setattr(Settings, "WEB_SEARCH_BACKEND", "fake")

    tool.execute("python", limit=Settings.WEB_SEARCH_LIMIT_MAX + 1)

    assert provider.search_limits == [Settings.WEB_SEARCH_LIMIT_MAX]


def test_web_search_normalizes_supports_search_exception(monkeypatch) -> None:
    """验证 Provider 搜索能力检查异常会归一化为工具错误。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: 能力检查异常逃逸或错误观察不符合契约时抛出。

    副作用:
        临时指定 fake Provider 为当前搜索后端。
    """

    registry = WebProviderRegistry()
    registry.register(FakeSearchProvider(fail_on="supports_search"))
    tool = WebSearchTool(provider_registry=registry)
    monkeypatch.setattr(Settings, "WEB_SEARCH_BACKEND", "fake")

    observation = tool.execute("python", limit=1)

    assert observation.status == "error"
    assert observation.error == "Web search failed using provider 'fake'."


def test_web_search_normalizes_is_available_exception(monkeypatch) -> None:
    """验证 Provider 配置可用性检查异常会归一化为工具错误。

    参数:
        monkeypatch: pytest 提供的模块属性替换工具。

    返回:
        无。

    异常:
        AssertionError: 可用性检查异常逃逸或错误观察不符合契约时抛出。

    副作用:
        临时指定 fake Provider 为当前搜索后端。
    """

    registry = WebProviderRegistry()
    registry.register(FakeSearchProvider(fail_on="is_available"))
    tool = WebSearchTool(provider_registry=registry)
    monkeypatch.setattr(Settings, "WEB_SEARCH_BACKEND", "fake")

    observation = tool.execute("python", limit=1)

    assert observation.status == "error"
    assert observation.error == "Web search failed using provider 'fake'."
