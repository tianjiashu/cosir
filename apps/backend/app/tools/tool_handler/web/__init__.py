"""Web provider abstractions used by web tool handlers."""

from app.tools.tool_handler.web.web_provider import (
    WebExtractItem,
    WebProvider,
    WebProviderUnavailableError,
    WebSearchItem,
)
from app.tools.tool_handler.web.web_provider_registry import (
    WebProviderRegistry,
    get_active_extract_provider,
    get_active_search_provider,
    register_default_web_providers,
)

__all__ = [
    "WebExtractItem",
    "WebProvider",
    "WebProviderRegistry",
    "WebProviderUnavailableError",
    "WebSearchItem",
    "get_active_extract_provider",
    "get_active_search_provider",
    "register_default_web_providers",
]
