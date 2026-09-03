"""CodeGraph 查询工具 handler（6 个 agent 可见只读工具，按 name 分发）。

单一职责：承载 codegraph_explore / search / node / callers / callees / impact 六个
只读查询工具的声明与执行。所有工具的执行逻辑同构——把模型参数组装为 vendor
method 的 params（含 workspace_path），经 ``CodeGraphKernelClient.query`` 调 Kernel，
透传返回文本。用单一 handler + ``for_tool`` 按 name 分发，避免 6 份重复代码。

与 vendor 的交互（见 toolsystem-integration-design §2.2/4.2）：
- 每个工具对应 vendor ``QUERY_METHODS`` 中一个 method（explore/search/node/...）。
- 参数名从模型的 snake_case 映射为 vendor 的 camelCase（如 max_files→maxFiles）。
- workspace_path 从 ``execution_context.workspace_root`` 注入，跨 workspace 隔离。
- 给模型的 ``content`` 恒为 vendor 原始文本（不因展示结构化而改变模型可见内容）。

职责边界：
- 负责：参数→vendor params 组装、Kernel 调用、文本聚合、成功/失败观察构造。
- 不负责：索引生命周期（status/init/sync 归后端自动管理，不暴露）、结果文本的解析
  （委托 ``result_parser``）、渲染（客户端负责）、同步触发（索引新鲜度由 turn 前
  ensure_ready 保证）。
"""

from typing import Any, ClassVar

from pydantic import BaseModel

from app.codegraph import CodeGraphKernelClient, CodeGraphKernelError
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.codegraph_query.result_parser import parse_tool_result
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import (
    CodegraphCalleesArgs,
    CodegraphCallersArgs,
    CodegraphExploreArgs,
    CodegraphImpactArgs,
    CodegraphNodeArgs,
    CodegraphSearchArgs,
)

# ----------------------------------------------------------------------
# 工具描述（面向模型，先讲何时用再讲怎么用，明确回退）
# ----------------------------------------------------------------------

_CODE_GRAPH_EXPLORE_DESCRIPTION: str = (
    "Understand code structure, locate implementations, trace call paths, or assess impact — "
    "the PRIMARY tool, call FIRST for almost any code question or before an edit. "
    "Query can be a natural-language question OR a bag of symbol/file names. "
    "Returns the verbatim source of the relevant symbols grouped by file in ONE call "
    "(Read-equivalent — treat the shown source as already Read; do NOT re-open those files), "
    "plus the call path among them. Usually the ONLY call you need — more accurate context, "
    "in far fewer tokens than a search/Read/Grep loop. "
    "Only fall back to search_files/read_file when you need pure-text/regex matching or CodeGraph "
    "is unavailable."
)

_CODE_GRAPH_SEARCH_DESCRIPTION = (
    "Quick symbol search by name; returns locations only (no code). Use to find where a symbol "
    "is defined, then feed the result to codegraph_node to read the source, or use "
    "codegraph_explore to understand an area in one call. Fall back to search_files for "
    "text/regex search."
)

_CODE_GRAPH_NODE_DESCRIPTION = (
    "Read a file or a single symbol (served from the code index). "
    "(1) READ A FILE — pass only `file` (path or basename), like Read: returns the file's current "
    "on-disk source with line numbers + which files depend on it; narrowable with offset/limit. "
    "Use it whenever you would Read a source file. "
    "(2) ONE SYMBOL — pass `symbol`: its location, signature, source (include_code=true) and "
    "caller/callee trail in one call, so before changing it you see what calls it. "
    "For an ambiguous name it returns every matching definition; pass file/line to pin one. "
    "Use codegraph_explore for several related symbols or the full flow."
)

_CODE_GRAPH_CALLERS_DESCRIPTION = (
    "List functions that call a given symbol. Use before changing a symbol to see who depends on "
    "it. For the full call flow, use codegraph_explore."
)

_CODE_GRAPH_CALLEES_DESCRIPTION = (
    "List functions that a given symbol calls. Use to see what a symbol depends on. "
    "For the full call flow, use codegraph_explore."
)

_CODE_GRAPH_IMPACT_DESCRIPTION = (
    "List symbols affected by changing a given symbol. Use before a refactor to assess the blast "
    "radius."
)


class CodegraphQueryTool(HandlerBase):
    """CodeGraph 只读查询工具（按 name 分发到 vendor method）。

    参数:
        name: 工具名（codegraph_explore 等）。
        args_model: 对应工具的参数模型。
        description: 面向模型的工具描述。
        client: 可选的 Kernel RPC 客户端；None 表示 Kernel 不可用，execute 降级。
    """

    # name / description / args_model 因「一个 handler 按 name 分发 6 个工具」需按实例不同，
    # 在 __init__ 中实例赋值，与父类 HandlerBase 放开为实例属性的声明一致。
    name: str
    description: str
    args_model: type[BaseModel]
    permission: ClassVar[str] = "codegraph_query"
    timeout_seconds: ClassVar[float] = 30.0
    risk_level: ClassVar[str] = "low"

    # snake_case(模型) → vendor camelCase 字段映射，按工具归类。
    _FIELD_MAP: ClassVar[dict[str, dict[str, str]]] = {
        "codegraph_explore": {"max_files": "maxFiles"},
        "codegraph_search": {},
        "codegraph_node": {
            "include_code": "includeCode",
            "symbols_only": "symbolsOnly",
        },
        "codegraph_callers": {},
        "codegraph_callees": {},
        "codegraph_impact": {},
    }

    def __init__(
        self,
        name: str,
        args_model: type[BaseModel],
        description: str,
        client: CodeGraphKernelClient | None = None,
    ) -> None:
        """构造 CodeGraph 查询工具实例。

        参数:
            name: 工具名。
            args_model: 参数模型。
            description: 面向模型的描述。
            client: Kernel RPC 客户端；None 表示 Kernel 不可用（execute 降级）。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        self.name = name
        self.args_model = args_model
        self.description = description
        self._client = client

    @classmethod
    def for_tool(
        cls,
        name: str,
        args_model: type[BaseModel],
        description: str,
        client: CodeGraphKernelClient | None = None,
    ) -> "CodegraphQueryTool":
        """按工具名构造实例（避免 6 个重复子类）。

        参数:
            name: 工具名（codegraph_explore 等）。
            args_model: 对应参数模型。
            description: 工具描述。
            client: 可选 Kernel client。

        返回:
            CodegraphQueryTool 实例。
        """
        return cls(name, args_model, description, client)

    def execute(self, *args: Any, **kwargs: Any) -> ToolObservation:
        """执行 CodeGraph 查询，返回结构化观测结果。

        处理顺序：无 workspace → Kernel 不可用 → 组装 params → client.query → 聚合文本
        → 解析为展示用结构化数据。

        参数:
            args / kwargs: 由 ToolExecutor 按 args_model 解包后的参数（snake_case）
                与 execution_context（最后注入）。

        返回:
            成功/失败均归一化为 ToolObservation。成功时 ``content`` 为 vendor 原始文本
            （模型消费），``data["codegraph"]`` 为展示用结构化数据（客户端消费，
            解析失败自动降级为 ``{"raw": ...}``，不影响 content）。

        异常:
            不主动抛出；Kernel 错误归一化为 tool_error。

        副作用:
            发起一次 CodeGraph 查询 RPC；结果解析失败时经 result_parser 写 warn 日志。
        """
        execution_context: ToolExecutionContext | None = kwargs.pop("execution_context", None)

        # 1. 无 workspace 降级：workspace_payload 不在 _WORKSPACE_REQUIRED_PERMISSIONS，
        #    execution_context 可能为 None；vendor 强制要求非空 workspace_path。
        if execution_context is None or execution_context.workspace_root is None:
            return tool_error(
                self.name,
                "CodeGraph query requires a workspace",
                reason=(
                    "There is no associated workspace for this query, so CodeGraph cannot run. "
                    "Use CodeGraph tools inside a workspace, or fall back to "
                    "search_files/read_file."
                ),
                permission=self.permission,
            )
        # 2. Kernel 不可用降级。
        if self._client is None:
            return tool_error(
                self.name,
                "CodeGraph unavailable",
                reason=(
                    "The CodeGraph Kernel is not ready, so this query cannot run. "
                    "Fall back to search_files/read_file."
                ),
                permission=self.permission,
            )

        workspace_path = str(execution_context.workspace_root)
        # 3. 组装 params：snake_case → vendor camelCase + workspace_path。
        field_map = self._FIELD_MAP.get(self.name, {})
        params: dict[str, Any] = {}
        for key, value in kwargs.items():
            params[field_map.get(key, key)] = value
        params["workspace_path"] = workspace_path

        try:
            result = self._client.query(self.name, params)
        except CodeGraphKernelError as exc:
            return tool_error(
                self.name,
                f"CodeGraph query failed: {exc}",
                reason=(
                    "The CodeGraph query could not complete. This may be transient "
                    "(retry may succeed) or the index may be unavailable — if it persists, "
                    "fall back to search_files/read_file."
                ),
                retryable=exc.retryable if hasattr(exc, "retryable") else False,
                permission=self.permission,
            )

        # 4. 聚合文本：result.content = [{type, text}...]。
        content = "\n".join(
            block["text"] for block in result.content if block.get("type") == "text"
        )
        if not content:
            content = "No results from CodeGraph query."
        # 5. 展示用结构化：仅进 data（客户端渲染），content 保持原文供模型消费。
        # data 仅保留 codegraph 子结构：旧方案中的 tool/query_params 为无用透传，
        # 前端只读 data.codegraph.items / data.codegraph.notice，保留会累积 SSE 载荷技术债，故删除。
        return tool_success(
            self.name,
            self.permission,
            content=content,
            data={"codegraph": parse_tool_result(self.name, content)},
        )

    def to_definition(self) -> ToolDefinition:
        """把工具实例转换成 ToolDefinition。

        参数:
            无。

        返回:
            可直接注册到 ToolRegistry 的工具定义。

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
            resource_keys=("workspace_payload",),
            display=ToolDisplayHints(
                verb="代码语义查询" if self.name == "codegraph_explore" else "代码关系查询",
                icon="network",
                expandable=True,
                expand_layout="list",
            ),
        )


# ----------------------------------------------------------------------
# 工厂函数（6 个）
# ----------------------------------------------------------------------


def build_codegraph_explore_definition(
    client: CodeGraphKernelClient | None = None,
) -> ToolDefinition:
    """构造 codegraph_explore 工具定义。"""
    return CodegraphQueryTool.for_tool(
        "codegraph_explore", CodegraphExploreArgs, _CODE_GRAPH_EXPLORE_DESCRIPTION, client
    ).to_definition()


def build_codegraph_search_definition(
    client: CodeGraphKernelClient | None = None,
) -> ToolDefinition:
    """构造 codegraph_search 工具定义。"""
    return CodegraphQueryTool.for_tool(
        "codegraph_search", CodegraphSearchArgs, _CODE_GRAPH_SEARCH_DESCRIPTION, client
    ).to_definition()


def build_codegraph_node_definition(
    client: CodeGraphKernelClient | None = None,
) -> ToolDefinition:
    """构造 codegraph_node 工具定义。"""
    return CodegraphQueryTool.for_tool(
        "codegraph_node", CodegraphNodeArgs, _CODE_GRAPH_NODE_DESCRIPTION, client
    ).to_definition()


def build_codegraph_callers_definition(
    client: CodeGraphKernelClient | None = None,
) -> ToolDefinition:
    """构造 codegraph_callers 工具定义。"""
    return CodegraphQueryTool.for_tool(
        "codegraph_callers", CodegraphCallersArgs, _CODE_GRAPH_CALLERS_DESCRIPTION, client
    ).to_definition()


def build_codegraph_callees_definition(
    client: CodeGraphKernelClient | None = None,
) -> ToolDefinition:
    """构造 codegraph_callees 工具定义。"""
    return CodegraphQueryTool.for_tool(
        "codegraph_callees", CodegraphCalleesArgs, _CODE_GRAPH_CALLEES_DESCRIPTION, client
    ).to_definition()


def build_codegraph_impact_definition(
    client: CodeGraphKernelClient | None = None,
) -> ToolDefinition:
    """构造 codegraph_impact 工具定义。"""
    return CodegraphQueryTool.for_tool(
        "codegraph_impact", CodegraphImpactArgs, _CODE_GRAPH_IMPACT_DESCRIPTION, client
    ).to_definition()
