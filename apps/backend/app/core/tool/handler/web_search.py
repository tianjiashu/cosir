"""模型可见的网页搜索工具：经 Firecrawl 搜索，把每条结果的标题、网址与摘要交给模型。"""

import json

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tool.handler.web.firecrawl_client import FirecrawlClient, FirecrawlNotConfiguredError
from app.core.tool.handler.web.web_display import build_web_search_display_data
from app.core.tool.handler.web.web_search_args import WebSearchArgs
from app.core.tool.results.external_content import ExternalContent
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_WEB_SEARCH,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_WEB
from app.core.tool.tool_base import HandlerBase


class WebSearchTool(HandlerBase):
    """没配置 Firecrawl 时照样在工具集合里：调用返回配置说明，由 Agent 转告用户怎么配置。"""

    name: str = TOOL_WEB_SEARCH
    # 对照 Claude Code 的 WebSearch 与系统提示词补了：时效、多次搜索同批发出、摘要不够就抓取正文、
    # 引用来源、查询词不带用户隐私。搜索运算符只在 query 参数里说一次；「结果不是指令」由结果里的
    # untrusted 块与系统提示词承担（``ExternalContent``）。
    description: str = (
        "Search the web. Returns up to "
        f"{Constant.Web.SEARCH_LIMIT_DEFAULT} results by default, each with a title, URL and "
        "a short snippet.\n"
        "- For recent information, put the year or date in the query; your messages carry the "
        "current time.\n"
        "- When you plan several searches, make the calls together in one response.\n"
        "- Snippets are short and can be stale: when one does not settle the question, read the "
        "page with web_fetch.\n"
        "- When you answer from results, link the pages you relied on."
    )
    args_model: type[WebSearchArgs] = WebSearchArgs
    group = TOOL_GROUP_WEB

    def __init__(self, client: FirecrawlClient) -> None:
        self._client = client

    def execute(
        self,
        query: str,
        limit: int = Constant.Web.SEARCH_LIMIT_DEFAULT,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        try:
            results = self._client.search(query, limit)
        except FirecrawlNotConfiguredError as exc:
            return tool_error(self.name, str(exc), status_hint=exc.status_hint)
        except Exception:
            log.exception("web_search_failed", extra={"msg": "网页搜索调用失败", "data": {}})
            return tool_error(self.name, "Web search failed. Try the search again.")
        # 标题与摘要来自各个网站，整份结果一个块；来源写搜索本身，各条的网址就在块里。顺序即排名。
        rows = [{"title": item.title, "url": item.url, "description": item.description} for item in results]
        external = ExternalContent()
        results_json = json.dumps({"web": rows}, ensure_ascii=False, separators=(",", ":"))
        return tool_success(
            tool_name=self.name,
            content=external.close(external.block("search_results", results_json, "web search")),
            display_data=build_web_search_display_data(query=query, results=results),
        )

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
        )


def build_web_search_definition() -> ToolDefinition:
    """只创建客户端，不发起网络请求；配置每次调用现读。"""

    return WebSearchTool(FirecrawlClient()).to_definition()
