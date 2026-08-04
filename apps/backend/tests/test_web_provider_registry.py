from dataclasses import dataclass

from app.config.settings import Settings
from app.tools.tool_handler.web.web_provider import WebExtractItem, WebSearchItem
from app.tools.tool_handler.web.web_provider_registry import (
    WebProviderRegistry,
    get_active_extract_provider,
    get_active_search_provider,
)


@dataclass
class FakeProvider:
    """用于注册表选择测试的完整 Web Provider 替身。"""

    name: str
    available: bool = True
    search_capability: bool = True
    extract_capability: bool = True

    @property
    def display_name(self) -> str:
        """返回用于测试诊断信息的 Provider 显示名称。

        参数:
            无。

        返回:
            与稳定 Provider 名称相同的显示名称。

        异常:
            无。

        副作用:
            无。
        """

        return self.name

    def is_available(self) -> bool:
        """返回测试替身配置的本地可用状态。

        参数:
            无。

        返回:
            ``available`` 字段的当前值。

        异常:
            无。

        副作用:
            无。
        """

        return self.available

    def supports_search(self) -> bool:
        """返回测试替身配置的搜索能力状态。

        参数:
            无。

        返回:
            ``search_capability`` 字段的当前值。

        异常:
            无。

        副作用:
            无。
        """

        return self.search_capability

    def supports_extract(self) -> bool:
        """返回测试替身配置的正文提取能力状态。

        参数:
            无。

        返回:
            ``extract_capability`` 字段的当前值。

        异常:
            无。

        副作用:
            无。
        """

        return self.extract_capability

    def supported_extract_formats(self) -> frozenset[str]:
        """返回测试替身声明支持的正文格式集合。

        参数:
            无。

        返回:
            具备提取能力时返回 Markdown 格式集合，否则返回空集合。

        异常:
            无。

        副作用:
            无。
        """

        return frozenset({"markdown"}) if self.extract_capability else frozenset()

    def missing_configuration_message(self) -> str:
        """返回测试替身未配置时的英文诊断信息。

        参数:
            无。

        返回:
            包含 Provider 名称的英文未配置说明。

        异常:
            无。

        副作用:
            无。
        """

        return f"{self.name} is not configured"

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        """实现完整 Provider 契约中的搜索入口。

        参数:
            query: 测试调用传入的搜索关键词。
            limit: 测试调用传入的结果数量上限。

        返回:
            始终为空的搜索结果列表。

        异常:
            无。

        副作用:
            无。
        """

        return []

    def extract(
        self,
        urls: list[str],
        output_format: str,
        char_limit: int,
    ) -> list[WebExtractItem]:
        """实现完整 Provider 契约中的正文提取入口。

        参数:
            urls: 测试调用传入的网页地址列表。
            output_format: 测试调用传入的网页正文格式。
            char_limit: 测试调用传入的单页字符上限。

        返回:
            始终为空的正文提取结果列表。

        异常:
            无。

        副作用:
            无。
        """

        return []


def test_explicit_search_backend_wins_even_when_unavailable() -> None:
    """验证显式搜索后端在本地不可用时仍优先返回以支持精确诊断。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 显式后端未被返回时由断言抛出。

    副作用:
        创建并填充仅属于当前测试的 Provider 注册表。
    """

    registry = WebProviderRegistry()
    registry.register(FakeProvider("tavily", available=False))
    registry.register(FakeProvider("exa", available=True))

    provider = registry.active_search_provider(explicit_backend="tavily")

    assert provider is not None
    assert provider.name == "tavily"


def test_extract_rejects_search_only_explicit_backend_without_fallback() -> None:
    """验证显式正文后端即使缺少能力也不会悄然回退到其他 Provider。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 显式 Provider 或其能力状态不符合预期时由断言抛出。

    副作用:
        创建并填充仅属于当前测试的 Provider 注册表。
    """

    registry = WebProviderRegistry()
    registry.register(FakeProvider("brave-free", available=True, extract_capability=False))
    registry.register(FakeProvider("firecrawl", available=True, extract_capability=True))

    provider = registry.active_extract_provider(explicit_backend="brave-free")

    assert provider is not None
    assert provider.name == "brave-free"
    assert provider.supports_extract() is False


def test_fallback_uses_legacy_priority_filtered_by_capability_and_availability() -> None:
    """验证回退路径仅从可用且具备对应能力的 Provider 中按既定优先级选择。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 搜索或提取 Provider 未按预期选择时由断言抛出。

    副作用:
        创建并填充仅属于当前测试的 Provider 注册表。
    """

    registry = WebProviderRegistry()
    registry.register(FakeProvider("exa", available=True, extract_capability=True))
    registry.register(FakeProvider("firecrawl", available=False, extract_capability=True))
    registry.register(FakeProvider("brave-free", available=True, extract_capability=False))

    assert registry.active_search_provider(explicit_backend="").name == "exa"
    assert registry.active_extract_provider(explicit_backend="").name == "exa"


def test_settings_helpers_prefer_capability_specific_backends() -> None:
    """验证 Settings 辅助函数优先使用搜索和提取各自的显式后端。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 辅助函数未返回对应显式后端时由断言抛出。

    副作用:
        临时覆盖全局 Settings，并在测试结束前恢复原值；创建内存 Provider 注册表。
    """

    original_search_backend = Settings.WEB_SEARCH_BACKEND
    original_extract_backend = Settings.WEB_EXTRACT_BACKEND
    original_backend = Settings.WEB_BACKEND
    registry = WebProviderRegistry()
    registry.register(FakeProvider("tavily", available=False))
    registry.register(FakeProvider("firecrawl", available=False))

    try:
        Settings.override(
            WEB_SEARCH_BACKEND="tavily",
            WEB_EXTRACT_BACKEND="firecrawl",
            WEB_BACKEND="exa",
        )

        assert get_active_search_provider(registry).name == "tavily"
        assert get_active_extract_provider(registry).name == "firecrawl"
    finally:
        Settings.override(
            WEB_SEARCH_BACKEND=original_search_backend,
            WEB_EXTRACT_BACKEND=original_extract_backend,
            WEB_BACKEND=original_backend,
        )
