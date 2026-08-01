from dataclasses import dataclass

from app.tools.tool_handler.web.web_provider import WebSearchItem
from app.tools.tool_handler.web.web_provider_registry import WebProviderRegistry


@dataclass
class FakeProvider:
    name: str
    available: bool = True
    search_capability: bool = True
    extract_capability: bool = True

    @property
    def display_name(self) -> str:
        return self.name

    def is_available(self) -> bool:
        return self.available

    def supports_search(self) -> bool:
        return self.search_capability

    def supports_extract(self) -> bool:
        return self.extract_capability

    def missing_configuration_message(self) -> str:
        return f"{self.name} is not configured"

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        return []


def test_explicit_search_backend_wins_even_when_unavailable() -> None:
    registry = WebProviderRegistry()
    registry.register(FakeProvider("tavily", available=False))
    registry.register(FakeProvider("exa", available=True))

    provider = registry.active_search_provider(explicit_backend="tavily")

    assert provider is not None
    assert provider.name == "tavily"


def test_extract_rejects_search_only_explicit_backend_without_fallback() -> None:
    registry = WebProviderRegistry()
    registry.register(FakeProvider("brave-free", available=True, extract_capability=False))
    registry.register(FakeProvider("firecrawl", available=True, extract_capability=True))

    provider = registry.active_extract_provider(explicit_backend="brave-free")

    assert provider is not None
    assert provider.name == "brave-free"
    assert provider.supports_extract() is False


def test_fallback_uses_legacy_priority_filtered_by_capability_and_availability() -> None:
    registry = WebProviderRegistry()
    registry.register(FakeProvider("exa", available=True, extract_capability=True))
    registry.register(FakeProvider("firecrawl", available=False, extract_capability=True))
    registry.register(FakeProvider("brave-free", available=True, extract_capability=False))

    assert registry.active_search_provider(explicit_backend="").name == "exa"
    assert registry.active_extract_provider(explicit_backend="").name == "exa"
