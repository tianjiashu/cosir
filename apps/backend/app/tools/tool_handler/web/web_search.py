"""模型可见的网页搜索工具。"""

import json
from typing import Any, ClassVar

from app.config.logging.logger import log
from app.config.settings import Settings
from app.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_execute.tool_success import tool_success
from app.tools.tool_handler.tool_base import HandlerBase
from app.tools.tool_handler.web.providers import default_web_providers
from app.tools.tool_handler.web.web_provider_registry import (
    WebProviderRegistry,
    register_default_web_providers,
)
from app.tools.tool_models.web_search_args import WebSearchArgs


class WebSearchTool(HandlerBase):
    """调用已配置 Provider 并将搜索元数据返回给模型。"""

    name: ClassVar[str] = "web_search"
    description: ClassVar[str] = "Search the web and return concise result metadata."
    permission: ClassVar[str] = "network"
    args_model: ClassVar[type[WebSearchArgs]] = WebSearchArgs
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
            ``provider`` 的 JSON 观察结果；无可用 Provider 或调用失败时返回错误观察结果。

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
            if provider is None:
                return tool_error(
                    self.name,
                    "No web search provider configured.",
                    reason=(
                        "Configure a supported web search provider, then retry the search. "
                        "No network request was made."
                    ),
                    permission=self.permission,
                )
            if not provider.supports_search():
                return tool_error(
                    self.name,
                    f"Web search provider '{provider.name}' does not support search.",
                    reason=(
                        "Select a provider with search capability through WEB_SEARCH_BACKEND "
                        "or WEB_BACKEND, then retry."
                    ),
                    permission=self.permission,
                )
            if not provider.is_available():
                return tool_error(
                    self.name,
                    provider.missing_configuration_message(),
                    reason=(
                        "Configure the selected web search provider locally, then retry. "
                        "No network request was made."
                    ),
                    permission=self.permission,
                )
            results = provider.search(query, effective_limit)
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
                reason=(
                    "The provider could not complete the search. Retry once; if it keeps "
                    "failing, select another configured web search provider."
                ),
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
                {"success": True, "data": {"web": web_results}}, separators=(",", ":")
            ),
            display_data={"web": web_results},
        )

    def render_request_summary(self, arguments: dict[str, Any]) -> str:
        """返回网页搜索的执行前摘要。

        参数:
            arguments: 已通过参数校验的工具调用参数。

        返回:
            搜索关键词；缺失时返回空字符串。

        异常:
            无。

        副作用:
            无。
        """

        return str(arguments.get("query") or "")

    def render_result_summary(self, display_data: dict[str, Any]) -> str:
        """返回网页搜索的执行后摘要。

        参数:
            display_data: 工具观察中的客户端展示数据。

        返回:
            成功时为结果数量摘要，失败时为错误摘要。

        异常:
            无。

        副作用:
            无。
        """

        if display_data.get("status") == "error":
            return "error:" + str(display_data.get("error", ""))
        results = display_data.get("web", [])
        return f"{len(results) if isinstance(results, list) else 0} web results"

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
                title_summary=self.render_request_summary,
                result_summary=self.render_result_summary,
                expandable=True,
                expand_layout="list",
            ),
        )


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
    return WebSearchTool(provider_registry).to_definition()
