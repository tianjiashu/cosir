"""模型可见的网页正文提取工具。"""

import json
import time
from collections.abc import Callable, Iterable
from typing import ClassVar, Literal

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tool.handler.web.web_display import build_web_extract_display_data
from app.core.tool.schemas import (
    TOOL_WEB_EXTRACT,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.tool_grouping import TOOL_GROUP_WEB
from app.core.tool.tool_base import HandlerBase
from app.core.tool.handler.web.firecrawl_client import (
    FirecrawlClient,
    FirecrawlUnavailableError,
    WebExtractItem,
)
from app.core.tool.handler.web.url_safety import (
    is_safe_public_url,
    normalize_url_for_request,
    sensitive_query_param_name,
    url_contains_secret,
)
from app.core.tool.handler.web.web_content_store import (
    convert_base64_images_to_placeholders,
)
from app.core.tool.handler.web.web_extract_args import WebExtractArgs


class WebExtractTool(HandlerBase):
    """校验 URL 后经 Firecrawl 提取并返回清理后的网页正文。"""

    name: str = TOOL_WEB_EXTRACT
    description: str = (
        "Extract content from public web page URLs. Returns clean page content in "
        "markdown/html (no LLM summarization - fast). Also works with PDF URLs "
        "(arxiv papers, documents) - pass the PDF link directly. Each page is capped "
        "by the per-page char_limit (result marked truncated=true when hit); when the "
        "combined output exceeds the global tool output budget, the full text is saved "
        "to disk with a [output truncated; full output: <path>] marker you can "
        "read_file. If a URL fails, inspect the result and choose whether to use another "
        "URL."
    )
    args_model: type[WebExtractArgs] = WebExtractArgs
    timeout_seconds: ClassVar[float] = Constant.Web.REQUEST_TIMEOUT_SECONDS + 10
    group = TOOL_GROUP_WEB


    def __init__(
        self,
        client: FirecrawlClient,
        resolver: Callable[[str], Iterable[str]] | None = None,
    ) -> None:
        """``resolver`` 是 URL 安全校验用的主机名解析器，省略时使用系统 DNS。"""

        self._client = client
        self._resolver = resolver

    def execute(
        self,
        urls: list[str],
        format: Literal["markdown", "html"] = "markdown",
        char_limit: int | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """校验 URL、提取清理后的正文并交上层统一截断落盘。

        参数:
            urls: 待提取的 URL 字符串列表；重复项在发起请求前去重。
            format: 模型请求的正文格式；参数模型已限定为 Firecrawl 支持的 markdown / html。
            char_limit: 透传给 Firecrawl 的单页字符预算；省略时使用全局配置。
            execution_context: 当前工具执行上下文；由执行链强制注入，本工具不再自行
                落盘，超长正文由上层 :class:`ToolOutputBudget` 统一截断并保存 artifact。

        返回:
            成功（含部分成功）时返回含 URL、标题、正文和元数据的 JSON 观察结果，部分失败时附带
            ``partial`` 与 ``failed_count``；全部页面失败、输入、配置、安全检查或提取失败时返回
            错误观察结果。

        异常:
            不向上抛出；所有 URL、远端调用与结果构造错误都归一化为错误观察结果。

        副作用:
            全部 URL 通过安全检查后才调用 Firecrawl；失败路径写 ``web_extract_unconfigured``、
            ``web_extract_failed``、``web_extract_all_pages_failed``、
            ``web_extract_result_build_failed``。日志只记录数量、耗时与失败原因，不记录 URL 原文与
            网页正文（单页失败由客户端记 ``web_extract_page_failed``；URL 地址层面的拦截与 fake-ip
            放宽由 :func:`is_safe_public_url` 记录）。
        """

        if len(urls) > Constant.Web.EXTRACT_URL_LIMIT_MAX:
            return tool_error(
                self.name,
                (
                    "Web extraction accepts at most "
                    f"{Constant.Web.EXTRACT_URL_LIMIT_MAX} URLs per call."
                ),
                reason="split the URLs into smaller batches and call web_extract again.",
                retryable=True,
            )

        normalized_urls_or_error = self._validate_urls(urls)
        if isinstance(normalized_urls_or_error, ToolObservation):
            return normalized_urls_or_error

        effective_char_limit = char_limit or Constant.Web.EXTRACT_CHAR_LIMIT
        started = time.monotonic()
        try:
            extracted_items = self._client.extract(
                normalized_urls_or_error, format, effective_char_limit
            )
        except FirecrawlUnavailableError as exc:
            log.error(
                "web_extract_unconfigured",
                extra={"msg": "网页正文提取缺少 Firecrawl 配置", "data": {"error": str(exc)}},
            )
            return tool_error(
                self.name,
                str(exc),
                reason="configure Firecrawl locally before calling web_extract again.",
                retryable=False,
            )
        except Exception:
            log.exception("web_extract_failed", extra={"msg": "网页正文提取调用失败", "data": {}})
            return tool_error(
                self.name,
                "Web extraction failed.",
                reason="try the extraction again.",
                retryable=True,
            )
        try:
            results = self._build_results(extracted_items)
        except Exception:
            log.exception(
                "web_extract_result_build_failed",
                extra={"msg": "网页正文提取结果构造失败", "data": {}},
            )
            return tool_error(
                self.name,
                "Web extraction result assembly failed.",
                reason="try different source URLs.",
                retryable=False,
            )
        del execution_context
        failures = [str(item["error"]) for item in results if item.get("error")]
        if results and len(failures) == len(results):
            log.error(
                "web_extract_all_pages_failed",
                extra={
                    "msg": "网页正文提取全部页面失败",
                    "data": {
                        "failed_count": len(failures),
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                    },
                },
            )
            return tool_error(
                self.name,
                f"Web extraction failed for all {len(results)} URL(s): " + "; ".join(failures),
                reason="try fewer URLs or choose different source URLs.",
                retryable=True,
            )
        payload: dict[str, object] = {"results": results}
        if failures:
            payload["partial"] = True
            payload["failed_count"] = len(failures)
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return tool_success(
            tool_name=self.name,
            content=content,
            display_data=build_web_extract_display_data(
                urls=[
                    item["url"]
                    for item in results
                    if isinstance(item.get("url"), str) and item["url"]
                ],
                partial=bool(failures),
            ),
        )

    def _validate_urls(self, urls: list[str]) -> list[str] | ToolObservation:
        """将候选输入转换为已完成安全校验且去重的规范化 URL 列表。

        参数:
            urls: 原始 URL 字符串列表。

        返回:
            全部通过检查时返回规范化且去重（保序）的 URL；任一项为空、无效或不安全时
            返回错误观察结果。

        异常:
            不向上抛出；URL 格式、密钥、凭据参数、协议和网络边界失败均转换为错误观察结果。

        副作用:
            通过注入解析器或系统 DNS 解析每个候选主机名，但不调用 Firecrawl。
        """

        normalized_urls: list[str] = []
        for value in urls:
            extracted_url = value.strip()
            if not extracted_url:
                return self._blocked_url_error(
                    "Blocked: each item must be a non-empty URL string.",
                    "provide non-empty URL strings.",
                )
            normalized_url = normalize_url_for_request(extracted_url)
            sensitive_name = sensitive_query_param_name(normalized_url)
            if sensitive_name is not None:
                return self._blocked_url_error(
                    f"Blocked: URL contains credential-like query parameter '{sensitive_name}'.",
                    "remove credential-like query parameters and provide a public URL.",
                )
            if url_contains_secret(extracted_url) or url_contains_secret(normalized_url):
                return self._blocked_url_error(
                    "Blocked: URL appears to contain an embedded secret.",
                    "remove credentials or secret values and provide a public URL.",
                )
            is_safe, safety_error = is_safe_public_url(normalized_url, self._resolver)
            if not is_safe:
                return self._blocked_url_error(
                    safety_error,
                    "use a publicly routable HTTP(S) URL without credentials.",
                )
            if normalized_url in normalized_urls:
                continue
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
        )

    def _build_results(
        self,
        extracted_items: list[WebExtractItem],
    ) -> list[dict[str, object]]:
        """清理 Firecrawl 正文并构造模型可见的提取结果。

        参数:
            extracted_items: Firecrawl 返回的正文结果。

        返回:
            包含按 ``char_limit`` 截断后的正文、元数据与可选错误的结果字典
            列表。``truncated`` / ``error`` 仅在为真 / 非空时出现，避免常量噪声；
            超长正文的落盘由上层 :class:`ToolOutputBudget` 统一处理，本方法不写磁盘。

        异常:
            无。图片占位替换与字段投影均为纯函数，不会向上抛出。

        副作用:
            替换内联 base64 图片为文本占位符。
        """

        results: list[dict[str, object]] = []
        for item in extracted_items:
            result: dict[str, object] = {
                "url": item.url,
                "title": item.title,
                "content": convert_base64_images_to_placeholders(item.content),
                "metadata": item.metadata,
            }
            if item.truncated:
                result["truncated"] = True
            if item.error:
                result["error"] = item.error
            results.append(result)
        return results

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
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("network",),
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="网页正文提取",
                icon="globe",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )

def build_web_extract_definition() -> ToolDefinition:
    """构造 ``web_extract`` 工具定义；只创建客户端，不发起网络请求。"""

    return WebExtractTool(FirecrawlClient()).to_definition_if_avaliable()
