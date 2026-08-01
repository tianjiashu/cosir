"""模型可见的网页正文提取工具。"""

import asyncio
import inspect
import json
from collections.abc import Callable, Iterable
from typing import Any, ClassVar, Literal, cast

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
from app.tools.tool_handler.web.url_safety import (
    extract_url_from_item,
    is_safe_public_url,
    normalize_url_for_request,
    sensitive_query_param_name,
    url_contains_secret,
)
from app.tools.tool_handler.web.web_content_store import (
    convert_base64_images_to_placeholders,
    truncate_or_store_content,
)
from app.tools.tool_handler.web.web_provider import WebExtractItem, WebProvider
from app.tools.tool_handler.web.web_provider_registry import (
    WebProviderRegistry,
    register_default_web_providers,
)
from app.tools.tool_models.web_extract_args import WebExtractArgs


class WebExtractTool(HandlerBase):
    """验证 URL 后调用已配置 Provider 返回清理后的网页正文。"""

    name: ClassVar[str] = "web_extract"
    description: ClassVar[str] = "Extract cleaned page content from public web URLs."
    permission: ClassVar[str] = "network"
    args_model: ClassVar[type[WebExtractArgs]] = WebExtractArgs
    timeout_seconds: ClassVar[float] = Settings.WEB_REQUEST_TIMEOUT_SECONDS + 10
    risk_level: ClassVar[str] = "medium"

    def __init__(
        self,
        provider_registry: WebProviderRegistry,
        resolver: Callable[[str], Iterable[str]] | None = None,
    ) -> None:
        """初始化使用指定 Provider 注册表和可选 DNS 解析器的提取工具。

        参数:
            provider_registry: 当前进程专属的 Web Provider 注册表。
            resolver: URL 安全校验使用的可选主机名解析器；省略时使用系统 DNS。

        返回:
            无。

        异常:
            无。

        副作用:
            保留传入注册表和解析器引用，不注册 Provider 且不发起网络请求。
        """

        self._provider_registry = provider_registry
        self._resolver = resolver

    def execute(
        self,
        urls: list[object],
        format: Literal["markdown", "html", "text"] = "markdown",
        char_limit: int | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """校验 URL、提取清理后的正文并在超限时保存完整内容。

        参数:
            urls: URL 字符串，或含字符串 ``url`` / ``href`` 的搜索结果对象。
            format: 模型请求的正文格式；当前 Provider 契约统一返回其清理后的正文文本。
            char_limit: 单页直接传给模型的最大字符数；省略时使用全局配置。
            execution_context: 当前工具执行工作区边界；超限正文只能在该根目录内保存。

        返回:
            成功时返回含 URL、标题、清理正文、Provider、存储路径和元数据的 JSON 观察结果；
            输入、Provider 配置、安全检查或提取失败时返回错误观察结果。

        异常:
            不向上抛出；所有 URL、Provider、内容存储和异步执行错误都归一化为错误观察结果。

        副作用:
            在全部 URL 通过安全检查后调用 Provider，可能发起网络请求；当正文超限时仅在当前
            ``execution_context.workspace_root`` 内写入完整清理内容；Provider 失败会写结构化日志。
        """

        del format
        if execution_context is None:
            return tool_error(
                self.name,
                "Web extraction requires an execution context.",
                reason=(
                    "Provide the task workspace execution context before extracting content. "
                    "No network request was made."
                ),
                permission=self.permission,
            )
        if len(urls) > Settings.WEB_EXTRACT_URL_LIMIT_MAX:
            return tool_error(
                self.name,
                (
                    "Web extraction accepts at most "
                    f"{Settings.WEB_EXTRACT_URL_LIMIT_MAX} URLs per call."
                ),
                reason=(
                    "Split the request into smaller batches, then retry. "
                    "No network request was made."
                ),
                permission=self.permission,
            )

        normalized_urls_or_error = self._validate_urls(urls)
        if isinstance(normalized_urls_or_error, ToolObservation):
            return normalized_urls_or_error

        effective_char_limit = char_limit or Settings.WEB_EXTRACT_CHAR_LIMIT
        backend = Settings.WEB_EXTRACT_BACKEND or Settings.WEB_BACKEND
        provider = None
        try:
            provider = self._provider_registry.active_extract_provider(backend)
            if provider is None and not backend:
                providers = self._provider_registry.list_providers()
                if len(providers) == 1:
                    provider = providers[0]
            if provider is None:
                return tool_error(
                    self.name,
                    "No web extraction provider configured.",
                    reason=(
                        "Configure an extract-capable web provider, then retry. "
                        "No network request was made."
                    ),
                    permission=self.permission,
                )
            if not provider.supports_extract():
                return tool_error(
                    self.name,
                    self._search_only_provider_message(provider),
                    reason=(
                        "Select an extract-capable backend through WEB_EXTRACT_BACKEND or "
                        "WEB_BACKEND, then retry. No network request was made."
                    ),
                    permission=self.permission,
                )
            if not provider.is_available():
                return tool_error(
                    self.name,
                    provider.missing_configuration_message(),
                    reason=(
                        "Configure the selected web extraction provider locally, then retry. "
                        "No network request was made."
                    ),
                    permission=self.permission,
                )
            extracted_items = self._execute_provider_extract(
                provider,
                normalized_urls_or_error,
                effective_char_limit,
            )
            results = self._build_results(
                extracted_items,
                provider.name,
                execution_context,
                effective_char_limit,
            )
        except Exception:
            provider_name = (
                provider.name if provider is not None else backend or "selected provider"
            )
            log.exception(
                "web_extract_provider_failed",
                extra={
                    "msg": "网页正文提取 Provider 调用失败",
                    "data": {"provider": provider_name},
                },
            )
            return tool_error(
                self.name,
                f"Web extraction failed using provider '{provider_name}'.",
                reason=(
                    "The provider could not complete the extraction. Retry once; "
                    "if it keeps failing, "
                    "select another configured extract-capable web provider."
                ),
                retryable=True,
                permission=self.permission,
            )
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=json.dumps({"success": True, "results": results}, separators=(",", ":")),
            display_data={"web": results},
        )

    def _validate_urls(self, urls: list[object]) -> list[str] | ToolObservation:
        """将候选输入转换为已完成安全校验的规范化 URL 列表。

        参数:
            urls: 原始 URL 字符串或搜索结果对象列表。

        返回:
            全部通过检查时返回规范化 URL；任一项无效或不安全时返回错误观察结果。

        异常:
            不向上抛出；URL 格式、密钥、凭据参数、协议和网络边界失败均转换为错误观察结果。

        副作用:
            通过注入解析器或系统 DNS 解析每个候选主机名，但不调用 Web Provider。
        """

        normalized_urls: list[str] = []
        for value in urls:
            extracted_url = extract_url_from_item(value)
            if extracted_url is None:
                return self._blocked_url_error(
                    "Blocked: each item must contain a non-empty string URL or href.",
                    "Pass URL strings or search result objects with a non-empty url or href field.",
                )
            normalized_url = normalize_url_for_request(extracted_url)
            sensitive_name = sensitive_query_param_name(normalized_url)
            if sensitive_name is not None:
                return self._blocked_url_error(
                    f"Blocked: URL contains credential-like query parameter '{sensitive_name}'.",
                    "Remove credential-like query parameters and retry with a public URL.",
                )
            if url_contains_secret(extracted_url) or url_contains_secret(normalized_url):
                return self._blocked_url_error(
                    "Blocked: URL appears to contain an embedded secret.",
                    "Remove credentials or secret values from the URL and retry.",
                )
            is_safe, safety_error = is_safe_public_url(normalized_url, self._resolver)
            if not is_safe:
                return self._blocked_url_error(
                    safety_error,
                    "Use a publicly routable HTTP(S) URL without credentials, then retry.",
                )
            normalized_urls.append(normalized_url)
        return normalized_urls

    def _blocked_url_error(self, error: str, remediation: str) -> ToolObservation:
        """构造 URL 安全校验失败的确定性错误观察结果。

        参数:
            error: 面向模型的英文拦截原因。
            remediation: 面向模型的英文修正建议。

        返回:
            标记网络权限且不可重试的错误观察结果。

        异常:
            无。

        副作用:
            无。
        """

        return tool_error(
            self.name,
            error,
            reason=f"{remediation} No network request was made.",
            permission=self.permission,
        )

    def _execute_provider_extract(
        self,
        provider: WebProvider,
        urls: list[str],
        char_limit: int,
    ) -> list[WebExtractItem]:
        """调用同步或异步 Provider 的正文提取方法。

        参数:
            provider: 已通过能力与本地配置校验的 Web Provider。
            urls: 已完成安全检查的规范化 URL 列表。
            char_limit: 每页直接返回给模型的字符上限。

        返回:
            Provider 返回的网页提取结果列表。

        异常:
            Exception: Provider 调用或异步协程执行失败时向上抛出，由执行入口记录并归一化。

        副作用:
            可能发起 Provider 网络请求；异步 Provider 会在本方法创建的私有事件循环中执行。
        """

        result = provider.extract(urls, char_limit)
        if inspect.isawaitable(result):
            return asyncio.run(cast(Any, result))
        return result

    def _build_results(
        self,
        extracted_items: list[WebExtractItem],
        provider_name: str,
        execution_context: ToolExecutionContext,
        char_limit: int,
    ) -> list[dict[str, object]]:
        """清理 Provider 正文并构造模型可见的提取结果。

        参数:
            extracted_items: Provider 返回的正文结果。
            provider_name: 本次调用的 Provider 稳定名称。
            execution_context: 完整内容可写入的当前工作区边界。
            char_limit: 单页模型输出字符上限。

        返回:
            包含清理正文、存储定位和 Provider 元数据的结果字典列表。

        异常:
            ValueError: 内容存储路径逃逸工作区时由内容存储模块抛出。
            OSError: 创建目录或写入完整内容失败时由内容存储模块抛出。

        副作用:
            替换内联 base64 图片；正文超限时只在当前工作区内写入完整内容。
        """

        results: list[dict[str, object]] = []
        for item in extracted_items:
            if item.error:
                results.append(
                    {
                        "url": item.url,
                        "title": item.title,
                        "content": "",
                        "provider": provider_name,
                        "stored_path": "",
                        "truncated": False,
                        "metadata": item.metadata,
                        "error": item.error,
                    }
                )
                continue
            full_content = item.raw_content or item.content
            cleaned_content = convert_base64_images_to_placeholders(full_content)
            content, stored_content = truncate_or_store_content(
                cleaned_content,
                item.url,
                item.title,
                execution_context,
                char_limit,
            )
            results.append(
                {
                    "url": item.url,
                    "title": item.title,
                    "content": content,
                    "provider": provider_name,
                    "stored_path": stored_content.relative_path if stored_content else "",
                    "truncated": stored_content is not None,
                    "metadata": item.metadata,
                }
            )
        return results

    def _search_only_provider_message(self, provider: WebProvider) -> str:
        """构造已配置搜索专用 Provider 的确定性错误文本。

        参数:
            provider: 不支持正文提取的已选 Provider。

        返回:
            任务约定的英文搜索专用后端错误文本。

        异常:
            无。

        副作用:
            无。
        """

        return (
            f"{provider.display_name} is a search-only backend and cannot extract URL content. "
            "Configure an extract-capable backend such as firecrawl, tavily, exa, or parallel."
        )

    def render_request_summary(self, arguments: dict[str, Any]) -> str:
        """返回网页正文提取的执行前摘要。

        参数:
            arguments: 已通过参数校验的工具调用参数。

        返回:
            请求 URL 数量的英文单行摘要。

        异常:
            无。

        副作用:
            无。
        """

        urls = arguments.get("urls")
        return f"{len(urls) if isinstance(urls, list) else 0} URL(s)"

    def render_result_summary(self, display_data: dict[str, Any]) -> str:
        """返回网页正文提取的执行后摘要。

        参数:
            display_data: 工具观察中的客户端展示数据。

        返回:
            成功时为提取页面数量摘要，失败时为错误摘要。

        异常:
            无。

        副作用:
            无。
        """

        if display_data.get("status") == "error":
            return "error:" + str(display_data.get("error", ""))
        results = display_data.get("web", [])
        return f"{len(results) if isinstance(results, list) else 0} extracted pages"

    def to_definition(self) -> ToolDefinition:
        """构造可注册的网页正文提取工具定义。

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
            resource_keys=("network", "filesystem"),
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="网页正文提取",
                icon="globe",
                title_summary=self.render_request_summary,
                result_summary=self.render_result_summary,
                expandable=True,
                expand_layout="list",
            ),
        )


def build_web_extract_definition() -> ToolDefinition:
    """构造使用内置 Provider 的网页正文提取工具定义。

    参数:
        无。

    返回:
        可直接注册到 ``ToolRegistry`` 的 ``web_extract`` 工具定义。

    异常:
        无。

    副作用:
        创建当前工具定义专属的 Provider 注册表及内置 Provider 实例，但不执行网络请求。
    """

    provider_registry = register_default_web_providers(
        WebProviderRegistry(),
        default_web_providers(),
    )
    return WebExtractTool(provider_registry).to_definition()
