"""Web provider registration and selection."""

from collections.abc import Iterable

from app.config.settings import Settings
from app.tools.tool_handler.web.web_provider import WebProvider

LEGACY_PROVIDER_PRIORITY = (
    "firecrawl",
    "parallel",
    "tavily",
    "exa",
    "searxng",
    "brave-free",
    "ddgs",
)


class WebProviderRegistry:
    """In-memory registry for web provider adapters."""

    def __init__(self) -> None:
        """初始化空的 Web Provider 注册表。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建仅属于当前实例的 Provider 映射。
        """

        self._providers: dict[str, WebProvider] = {}

    def register(self, provider: WebProvider) -> None:
        """注册或替换一个 Web Provider。

        参数:
            provider: 满足 ``WebProvider`` 契约的 Provider 实例。

        返回:
            无。

        异常:
            无。

        副作用:
            将 Provider 写入当前注册表；同名 Provider 会被替换。
        """

        self._providers[provider.name] = provider

    def list_providers(self) -> list[WebProvider]:
        """返回当前已注册的全部 Web Provider。

        参数:
            无。

        返回:
            按注册顺序排列的 Provider 列表副本。

        异常:
            无。

        副作用:
            无。
        """

        return list(self._providers.values())

    def get_provider(self, name: str) -> WebProvider | None:
        """按名称取得已注册的 Web Provider。

        参数:
            name: Provider 的稳定英文名称。

        返回:
            匹配的 Provider；不存在时返回 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        return self._providers.get(name)

    def active_search_provider(self, explicit_backend: str = "") -> WebProvider | None:
        """选择用于网页搜索的 Provider。

        参数:
            explicit_backend: 显式指定的 Provider 名称；非空时不进行能力或可用性回退筛选。

        返回:
            显式 Provider、首个可用的搜索 Provider，或没有匹配项时的 ``None``。

        异常:
            无。

        副作用:
            仅读取 Provider 的本地可用性状态，不发起网络请求。
        """

        return self._active_provider(explicit_backend, "supports_search")

    def active_extract_provider(self, explicit_backend: str = "") -> WebProvider | None:
        """选择用于网页正文提取的 Provider。

        参数:
            explicit_backend: 显式指定的 Provider 名称；非空时不进行能力或可用性回退筛选。

        返回:
            显式 Provider、首个可用的提取 Provider，或没有匹配项时的 ``None``。

        异常:
            无。

        副作用:
            仅读取 Provider 的本地可用性状态，不发起网络请求。
        """

        return self._active_provider(explicit_backend, "supports_extract")

    def _active_provider(
        self,
        explicit_backend: str,
        capability_name: str,
    ) -> WebProvider | None:
        """按显式配置或遗留优先级选择支持目标能力的 Provider。

        参数:
            explicit_backend: 显式指定的 Provider 名称。
            capability_name: ``WebProvider`` 上的能力判定方法名。

        返回:
            显式 Provider、首个可用且支持目标能力的 Provider，或 ``None``。

        异常:
            无。

        副作用:
            调用候选 Provider 的本地能力和可用性判定，不发起网络请求。
        """

        if explicit_backend:
            return self.get_provider(explicit_backend)

        for provider_name in LEGACY_PROVIDER_PRIORITY:
            provider = self.get_provider(provider_name)
            if provider is None:
                continue
            supports_capability = getattr(provider, capability_name)
            if supports_capability() and provider.is_available():
                return provider
        return None


def register_default_web_providers(
    registry: WebProviderRegistry,
    providers: Iterable[WebProvider] = (),
) -> WebProviderRegistry:
    """将调用方提供的默认 Web Provider 注册到实例级注册表。

    参数:
        registry: 承载默认 Provider 的实例级注册表。
        providers: 按注册顺序提供的默认 Provider；任务后续会传入具体适配器。

    返回:
        已完成注册的原注册表实例。

    异常:
        无。

    副作用:
        修改传入注册表的 Provider 映射。
    """

    for provider in providers:
        registry.register(provider)
    return registry


def get_active_search_provider(registry: WebProviderRegistry) -> WebProvider | None:
    """根据当前 Settings 选择网页搜索 Provider。

    参数:
        registry: 已注册 Provider 的实例级注册表。

    返回:
        当前显式配置或回退策略选出的搜索 Provider；无可用候选时返回 ``None``。

    异常:
        无。

    副作用:
        读取 ``Settings`` 和 Provider 的本地可用性状态，不发起网络请求。
    """

    return registry.active_search_provider(Settings.WEB_SEARCH_BACKEND or Settings.WEB_BACKEND)


def get_active_extract_provider(registry: WebProviderRegistry) -> WebProvider | None:
    """根据当前 Settings 选择网页正文提取 Provider。

    参数:
        registry: 已注册 Provider 的实例级注册表。

    返回:
        当前显式配置或回退策略选出的提取 Provider；无可用候选时返回 ``None``。

    异常:
        无。

    副作用:
        读取 ``Settings`` 和 Provider 的本地可用性状态，不发起网络请求。
    """

    return registry.active_extract_provider(Settings.WEB_EXTRACT_BACKEND or Settings.WEB_BACKEND)
