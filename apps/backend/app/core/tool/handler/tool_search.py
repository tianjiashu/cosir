"""tool_search 工具：把延迟工具的完整定义取进上下文，之后的请求就能直接调用它们。

行为与本仓对齐的 ToolSearch 一致（契约逐条见 ``handler/deferred/catalog_search``）：命中时成功观察的正文是
``<functions>`` 块，每个工具一行 ``<function>{...}</function>``，与请求里的定义逐字相同；没有命中也是成功，正文
``No matching deferred tools found``。块后一句说明这些工具已加载、可以按名字直接调用：定义只在这条结果里，
不进请求的工具列表（加载不打断提示词缓存，见 ``RequestTools``）。被取出的工具随观察的 ``facts`` 写进结果行
metadata（外部工具存定义快照、内置工具存名字），那就是「已加载」的唯一事实；同一批里随后调用刚加载的工具仍会
被拒，下一批才放行。

目录是 ``DeferredCatalog``：Session 固化的延迟内置工具（经 ``ToolExecutionContext.request_tools`` 交来）加上外部
工具。外部目录读不出来时内置工具照常可检索；什么都没命中时才把不可用的原因作为错误返回。

不重复加载：命中的工具已加载且定义没变时，不再给定义、不再记加载，只提醒模型它已加载、直接调用；外部工具的
定义变了（目录通知让模型重新加载）照常给新版本。
"""

import json

from app.config.logging.logger import log
from app.core.tool.handler.deferred.catalog_search import search_catalog
from app.core.tool.handler.deferred.deferred_catalog import DeferredCatalog, DeferredEntry
from app.core.tool.handler.deferred.tool_search_args import ToolSearchArgs
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import TOOL_SEARCH, ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tool.schemas.request_tools import RequestTools
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_MCP
from app.core.tool.tool_base import HandlerBase
from app.models.message_context.message_metadata.tool_message_metadata import ToolResultFacts

NO_MATCH = "No matching deferred tools found"
# 重复加载时的提醒：不再给一遍定义（也不再记一次加载），指回模型已有的那份。
_ALREADY_LOADED = (
    "Already loaded — their schemas are in an earlier tool_search result in this conversation. Do not load "
    "them again; call them directly by name:\n"
)
# 加载的定义只在这条结果里、不进请求的工具列表（见 ``RequestTools``），所以明说可以按名字直接调用。
_LOADED_NOTE = (
    "These tools are now loaded. Their definitions stay in this result instead of your tool list; call them "
    "directly by name with arguments matching the schemas above, exactly like your other tools."
)


class ToolSearchTool(HandlerBase):
    name: str = TOOL_SEARCH
    description: str = (
        "Fetches full schema definitions for deferred tools so they can be called.\n\n"
        "Deferred tools appear by name in a message from the system. Until fetched, only the name is "
        "known — there is no parameter schema, so the tool cannot be invoked. This tool takes a query, "
        "matches it against the deferred tool list, and returns the matched tools' complete JSONSchema "
        "definitions inside a <functions> block. Once a tool's schema appears in that result, it is "
        "callable exactly like any of your other tools.\n\n"
        'Result format: each matched tool appears as one <function>{"description": "...", "name": '
        '"...", "parameters": {...}}</function> line inside the <functions> block — the same encoding '
        "as your other tool definitions.\n\n"
        "Query forms:\n"
        '- "select:mcp__github__create_issue,mcp__github__get_issue" — fetch these exact tools by name\n'
        '- "notebook jupyter" — keyword search, up to max_results best matches\n'
        '- "+slack send" — require "slack" in the name, rank by remaining terms'
    )
    args_model: type[ToolSearchArgs] = ToolSearchArgs
    group = TOOL_GROUP_MCP

    def execute(
        self, query: str, max_results: int, execution_context: ToolExecutionContext
    ) -> ToolObservation:
        request_tools = execution_context.request_tools or RequestTools((), frozenset())
        catalog = DeferredCatalog.read(
            request_tools.deferred_schemas, str(execution_context.workspace_root)
        )
        matched = search_catalog(catalog.entries, query, max_results)
        if not matched:
            if catalog.unavailable is not None:
                return tool_error(
                    self.name,
                    catalog.unavailable,
                    tool_call_id=execution_context.tool_call_id,
                    status_hint="目录不可用",
                )
            return tool_success(self.name, NO_MATCH, tool_call_id=execution_context.tool_call_id)
        fresh = [entry for entry in matched if not self._already_loaded(entry, request_tools)]
        loaded = [entry.name for entry in matched if self._already_loaded(entry, request_tools)]
        if loaded:
            log.info(
                "tool_search_already_loaded",
                extra={
                    "msg": "检索命中已加载的延迟工具，不重复加载",
                    "data": {"tool_call_id": execution_context.tool_call_id, "tools": loaded},
                },
            )
        blocks = [self._render_functions(fresh)] if fresh else []
        if loaded:
            blocks.append(_ALREADY_LOADED + "\n".join(loaded))
        external = tuple(entry.external for entry in fresh if entry.external is not None)
        builtin = tuple(entry.name for entry in fresh if entry.external is None)
        return tool_success(
            self.name,
            "\n\n".join(blocks),
            tool_call_id=execution_context.tool_call_id,
            facts=ToolResultFacts(
                loaded_external_tools=external or None, loaded_builtin_tools=builtin or None
            )
            if fresh
            else None,
        )

    @staticmethod
    def _already_loaded(entry: DeferredEntry, request_tools: RequestTools) -> bool:
        """已加载且定义没变：内置工具的定义随工具面固化、不会变；外部工具要和加载时的快照一致——服务改了定义
        （目录通知已让模型重新加载），这次就该给新版本。"""
        if entry.external is None:
            return entry.name in request_tools.loaded_builtin
        return request_tools.external.get(entry.name) == entry.external

    @staticmethod
    def _render_functions(entries: list[DeferredEntry]) -> str:
        """``<functions>`` 块：每个工具一行，JSON 与请求里的定义同一份。"""
        lines = (
            f"<function>{json.dumps(entry.function, ensure_ascii=False)}</function>"
            for entry in entries
        )
        return "<functions>\n" + "\n".join(lines) + "\n</functions>\n" + _LOADED_NOTE

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
        )


def build_tool_search_definition() -> ToolDefinition:
    return ToolSearchTool().to_definition()
