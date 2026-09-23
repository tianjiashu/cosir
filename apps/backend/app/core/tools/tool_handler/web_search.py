"""模型可见的网页搜索工具。"""

import json
from typing import ClassVar

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.tools.display.web_display import build_web_search_display_data
from app.core.tools.schemas import (
    TOOL_WEB_SEARCH,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_handler.web.providers import default_web_providers
from app.core.tools.tool_handler.web.web_provider import WebProviderUnavailableError
from app.core.tools.tool_handler.web.web_provider_registry import (
    WebProviderRegistry,
    register_default_web_providers,
)
from app.core.tools.tool_models.web_search_args import WebSearchArgs


class WebSearchTool(HandlerBase):
    """调用已配置 Provider 并将搜索元数据返回给模型。"""

    name: str = TOOL_WEB_SEARCH
    description: str = (
        "Search the web for information. Returns up to 5 results by default with "
        "titles, URLs, and descriptions. The query is passed through to the configured "
        "backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, "
        'and "exact phrase" may work when the backend supports them.'
    )
    permission: ClassVar[str] = "network"
    args_model: type[WebSearchArgs] = WebSearchArgs
    timeout_seconds: ClassVar[float] = Settings.WEB_REQUEST_TIMEOUT_SECONDS + 5
    risk_level: ClassVar[str] = "medium"

    def __init__(self, provider_registry: WebProviderRegistry) -> None:
        """初始化使用指定 Provider 注册表的搜索工具。

        参数:
            provider_registry: 当前进程专属的 Web Provider 注册表。

        返回:
            无。

        异常:
            无。

        副作用:
            保留传入注册表的引用，不注册 Provider 且不发起网络请求。
        """

        self._provider_registry = provider_registry

    def execute(
        self,
        query: str,
        limit: int = 5,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """执行网页搜索并返回仅含元数据的结构化观察结果。

        参数:
            query: 非空搜索关键词。
            limit: 调用方请求的最大结果数，会被钳制到全局安全上限。
            execution_context: 本次工具执行上下文；本工具不消费该值，但保留以符合 handler 契约。

        返回:
            搜索成功时返回包含 ``title``、``url``、``description``、``position`` 和
            ``provider`` 的 JSON 观察结果（远端无结果时为合法的空列表）；无可用
            Provider、Provider 未配置或调用失败时返回错误观察结果，配置类失败标记
            ``retryable=False``。

        异常:
            不向上抛出；Provider 选择、可用性检查或搜索执行异常均归一化为错误观察结果。

        副作用:
            调用选中 Provider 的本地状态检查，并在成功条件满足时可能发起一次网络搜索。
            Provider 执行异常会记录结构化错误日志。
        """

        del execution_context
        effective_limit = min(limit, Settings.WEB_SEARCH_LIMIT_MAX)
        backend = Settings.WEB_SEARCH_BACKEND or Settings.WEB_BACKEND
        provider = None
        try:
            provider = self._provider_registry.active_search_provider(backend)
            results = provider.search(query, effective_limit)
        except WebProviderUnavailableError as exc:
            log.error(
                "web_search_provider_unavailable",
                extra={
                    "msg": "网页搜索 Provider 未配置",
                    "data": {"provider": provider.name if provider else backend, "error": str(exc)},
                },
            )
            return tool_error(
                self.name,
                str(exc),
                reason="configure the selected provider locally before continuing.",
                retryable=False,
                permission=self.permission,
            )
        except Exception:
            provider_name = (
                provider.name if provider is not None else backend or "selected provider"
            )
            log.exception(
                "web_search_provider_failed",
                extra={
                    "msg": "网页搜索 Provider 调用失败",
                    "data": {"provider": provider_name},
                },
            )
            return tool_error(
                self.name,
                f"Web search failed using provider '{provider_name}'.",
                reason="try the search again or choose another configured provider.",
                retryable=True,
                permission=self.permission,
            )
        web_results = [
            {
                "title": item.title,
                "url": item.url,
                "description": item.description,
                "position": item.position,
                "provider": provider.name,
            }
            for item in results
        ]
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=json.dumps(
                {"web": web_results},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            display_data=build_web_search_display_data(query=query, results=web_results),
        )

    def to_definition(self) -> ToolDefinition:
        """构造可注册的网页搜索工具定义。

        参数:
            无。

        返回:
            含模型参数、网络权限、线程执行模式和前端展示元数据的工具定义。

        异常:
            无。

        副作用:
            无。
        """

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=("network",),
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="网页搜索",
                icon="globe",
                surface="standalone",
                expandable=True,
                expand_layout="list",
                show_result=False,
            ),
        )

    def avaliable(self) -> bool:
        backend = Settings.WEB_SEARCH_BACKEND or Settings.WEB_BACKEND
        provider = self._provider_registry.active_search_provider(backend)
        if provider is None:
            if backend:
                return False
            return False
        if not provider.supports_search():
            return False
        if not provider.is_available():
            return False
        return True


def build_web_search_definition(
    provider_registry: WebProviderRegistry | None = None,
) -> ToolDefinition:
    """构造使用内置 Provider 的网页搜索工具定义。

    参数:
        provider_registry: 可选注入的 Provider 注册表；省略时新建并注册内置 Provider。
            测试可传入 fake 注册表以覆盖 Provider 选择而无需真实网络配置。

    返回:
        可直接注册到 ``ToolRegistry`` 的 ``web_search`` 工具定义。

    异常:
        无。

    副作用:
        当 ``provider_registry`` 为 None 时，创建当前工具定义专属的 Provider 注册表及
        内置 Provider 实例，但不执行网络请求。
    """

    if provider_registry is None:
        provider_registry = register_default_web_providers(
            WebProviderRegistry(),
            default_web_providers(),
        )
    return WebSearchTool(provider_registry).to_definition_if_avaliable()
