"""Built-in web provider adapters."""

from app.tools.tool_handler.web.providers.brave_provider import BraveProvider
from app.tools.tool_handler.web.providers.ddgs_provider import DdgsProvider
from app.tools.tool_handler.web.providers.exa_provider import ExaProvider
from app.tools.tool_handler.web.providers.firecrawl_provider import FirecrawlProvider
from app.tools.tool_handler.web.providers.parallel_provider import ParallelProvider
from app.tools.tool_handler.web.providers.searxng_provider import SearxngProvider
from app.tools.tool_handler.web.providers.tavily_provider import TavilyProvider
from app.tools.tool_handler.web.web_provider import WebProvider


def default_web_providers() -> list[WebProvider]:
    """构造按既定回退优先级排列的内置 Web Provider。

    参数:
        无。

    返回:
        未过滤可用性的 Provider 实例列表。

    异常:
        无。

    副作用:
        读取 Provider 构造时所需的本地环境变量。
    """

    return [
        FirecrawlProvider(),
        ParallelProvider(),
        TavilyProvider(),
        ExaProvider(),
        SearxngProvider(),
        BraveProvider(),
        DdgsProvider(),
    ]


__all__ = [
    "BraveProvider",
    "DdgsProvider",
    "ExaProvider",
    "FirecrawlProvider",
    "ParallelProvider",
    "SearxngProvider",
    "TavilyProvider",
    "default_web_providers",
]
