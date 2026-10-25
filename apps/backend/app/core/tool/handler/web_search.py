"""模型可见的网页搜索工具。"""

import json
from typing import ClassVar

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tool.handler.web.web_display import build_web_search_display_data
from app.core.tool.schemas import (
    TOOL_WEB_SEARCH,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.tool_grouping import TOOL_GROUP_WEB
from app.core.tool.tool_base import HandlerBase
from app.core.tool.handler.web.firecrawl_client import FirecrawlClient, FirecrawlUnavailableError
from app.core.tool.handler.web.web_search_args import WebSearchArgs


class WebSearchTool(HandlerBase):
    """经 Firecrawl 搜索并把结果元数据返回给模型。"""

    name: str = TOOL_WEB_SEARCH
    description: str = (
        "Search the web for information. Returns up to "
        f"{Constant.Web.SEARCH_LIMIT_DEFAULT} results by default with "
        "titles, URLs, and descriptions. The query is passed through to the search "
        "engine, so operators such as site:domain, filetype:pdf, intitle:word, -term, "
        'and "exact phrase" may work.'
    )
    args_model: type[WebSearchArgs] = WebSearchArgs
    timeout_seconds: ClassVar[float] = Constant.Web.REQUEST_TIMEOUT_SECONDS + 5
    group = TOOL_GROUP_WEB


    def __init__(self, client: FirecrawlClient) -> None:
        self._client = client

    def execute(
        self,
        query: str,
        limit: int = Constant.Web.SEARCH_LIMIT_DEFAULT,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """执行网页搜索，返回只含标题、URL、描述与排名的 JSON 观察（远端无结果时为空列表）。

        缺配置返回不可重试的错误观察；其余调用失败记录异常日志后返回可重试的错误观察，不向上抛出。
        """

        effective_limit = min(limit, Constant.Web.SEARCH_LIMIT_MAX)
        try:
            results = self._client.search(query, effective_limit)
        except FirecrawlUnavailableError as exc:
            log.error(
                "web_search_unconfigured",
                extra={"msg": "网页搜索缺少 Firecrawl 配置", "data": {"error": str(exc)}},
            )
            return tool_error(
                self.name,
                str(exc),
                reason="configure Firecrawl locally before continuing.",
                retryable=False,
            )
        except Exception:
            log.exception("web_search_failed", extra={"msg": "网页搜索调用失败", "data": {}})
            return tool_error(
                self.name,
                "Web search failed.",
                reason="try the search again.",
                retryable=True,
            )
        web_results = [
            {
                "title": item.title,
                "url": item.url,
                "description": item.description,
                "position": item.position,
            }
            for item in results
        ]
        return tool_success(
            tool_name=self.name,
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
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
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


def build_web_search_definition() -> ToolDefinition:
    """构造 ``web_search`` 工具定义；只创建客户端，不发起网络请求。"""

    return WebSearchTool(FirecrawlClient()).to_definition_if_avaliable()
