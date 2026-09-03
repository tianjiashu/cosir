"""Built-in web provider adapters."""

from app.core.tools.tool_handler.web.providers.firecrawl_provider import FirecrawlProvider
from app.core.tools.tool_handler.web.web_provider import WebProvider


def default_web_providers() -> list[WebProvider]:
    """构造按既定回退优先级排列的内置 Web Provider。

    第一阶段只内置 ``firecrawl`` 一条通路，覆盖「查 API 文档 / GitHub」核心场景：
    ``firecrawl`` 提供 search + extract 全能，配 ``FIRECRAWL_API_KEY`` 后全功能可用。

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
    ]


__all__ = [
    "FirecrawlProvider",
    "default_web_providers",
]
