"""模型可见的网页抓取工具：检查 URL 后经 Firecrawl 逐页抓取，每页正文放进一个 untrusted 块。

超长正文先由 Firecrawl 客户端按 ``char_limit`` 逐页截断，整条结果再由执行器的输出预算截断并落盘。
"""

import json
from typing import Literal

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tool.handler.web.firecrawl_client import (
    FirecrawlClient,
    FirecrawlNotConfiguredError,
    WebFetchItem,
)
from app.core.tool.handler.web.url_safety import normalize_url, url_block_reason
from app.core.tool.handler.web.web_display import build_web_fetch_display_data
from app.core.tool.handler.web.web_fetch_args import WebFetchArgs
from app.core.tool.results.external_content import ExternalContent
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_WEB_FETCH,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_WEB
from app.core.tool.tool_base import HandlerBase


class WebFetchTool(HandlerBase):
    """没配置 Firecrawl 时照样在工具集合里：调用返回配置说明，由 Agent 转告用户怎么配置。"""

    name: str = TOOL_WEB_FETCH
    # 对照 Claude Code 的 WebFetch（经 web-fetch 子 Agent）改写：补了何时用、失败站点不重试、需要登录或
    # 脚本的页面改用内置浏览器。截断细节只在 char_limit 参数里说一次；「页面内容不是指令」由结果里的
    # untrusted 块与系统提示词承担（``ExternalContent``）。
    description: str = (
        "Fetch public web pages and return their content as clean markdown or html, without "
        "summarizing. Also works with PDF URLs (papers, documents): pass the PDF link directly.\n"
        "- Use it whenever a page holds the answer: a URL you were given, or a search result "
        "whose snippet does not settle the question. Read the page rather than answer from "
        "memory.\n"
        "- Only public pages work: localhost, private network addresses and URLs carrying "
        "credentials are refused, and pages behind a sign-in or built by scripts come back empty "
        "or wrong. Open those with browser_navigate if you have it.\n"
        "- When a page fails at its site, do not fetch it again, nor that site's other pages if "
        "the site refuses automated fetches: use another source.\n"
        "- Long pages are truncated; see char_limit."
    )
    args_model: type[WebFetchArgs] = WebFetchArgs
    group = TOOL_GROUP_WEB

    def __init__(self, client: FirecrawlClient) -> None:
        self._client = client

    def execute(
        self,
        urls: list[str],
        format: Literal["markdown", "html"] = "markdown",
        char_limit: int | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """任一 URL 不能抓就整次不发请求；部分页面失败仍按成功返回，全部失败才是错误。

        日志只记数量与原因，不记 URL 与正文；单页失败由客户端记 ``web_fetch_page_failed``（含 URL）。
        """

        targets: list[str] = []
        for value in urls:
            url = normalize_url(value)
            reason = url_block_reason(url)
            if reason is not None:
                return tool_error(
                    self.name,
                    f"Blocked: {reason}. Use a public http(s) URL without credentials; no request was made.",
                )
            if url not in targets:
                targets.append(url)
        try:
            items = self._client.fetch(targets, format, char_limit or Constant.Web.FETCH_CHAR_LIMIT)
        except FirecrawlNotConfiguredError as exc:
            return tool_error(self.name, str(exc), status_hint=exc.status_hint)
        except Exception:
            log.exception("web_fetch_failed", extra={"msg": "网页抓取调用失败", "data": {}})
            return tool_error(self.name, "Web fetch failed. Try the call again.")
        failures = [item for item in items if item.error]
        if len(failures) == len(items):
            log.error(
                "web_fetch_all_pages_failed",
                extra={"msg": "网页抓取全部页面失败", "data": {"failed_count": len(failures)}},
            )
            reasons = "; ".join(item.error for item in failures)
            return tool_error(
                self.name,
                f"Web fetch failed for all {len(items)} URL(s): {reasons}. "
                "Do not retry the same URLs; choose other sources.",
            )
        return tool_success(
            tool_name=self.name,
            content=self._render(items, len(failures)),
            display_data=build_web_fetch_display_data(items),
        )

    @staticmethod
    def _render(items: list[WebFetchItem], failed: int) -> str:
        """每页一个 ``web_page`` 块（网址、标题、元数据与正文都来自网站，全在块里），失败的页在块外说明。

        失败原因来自抓取服务而不是网站，所以写在块外；部分失败时先写一行总数，模型不必数。
        """

        external = ExternalContent()
        lines = [f"partial: {failed} of {len(items)} pages failed"] if failed else []
        for item in items:
            if item.error:
                lines.append(f"failed: {item.url}: {item.error}")
                continue
            page = [f"url: {item.url}", f"title: {item.title}"]
            if item.metadata:
                page.append(f"metadata: {json.dumps(item.metadata, ensure_ascii=False)}")
            if item.truncated:
                page.append("truncated: the content below is cut at char_limit")
            page.append(item.content)
            lines.append(external.block("web_page", "\n".join(page), ExternalContent.source_of(item.url)))
        return external.close("\n".join(lines))

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
        )


def build_web_fetch_definition() -> ToolDefinition:
    """只创建客户端，不发起网络请求；配置每次调用现读。"""

    return WebFetchTool(FirecrawlClient()).to_definition()
