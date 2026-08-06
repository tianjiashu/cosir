"""CodeGraph 查询工具 handler 测试。

覆盖 6 个工具 execute：
- workspace_path 从 execution_context.workspace_root 注入 params；
- 成功聚合文本；Kernel 不可用（client=None）降级；query 抛错降级；
- 无 workspace（execution_context=None）降级；
- 按 name 路由到对应 vendor method + snake_case→camelCase 参数转换。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.codegraph import CodeGraphKernelUnavailableError
from app.codegraph.protocol import QueryResult
from app.tools.schemas import ToolExecutionContext
from app.tools.tool_handler.codegraph_query import (
    CodegraphQueryTool,
    build_codegraph_callees_definition,
    build_codegraph_callers_definition,
    build_codegraph_explore_definition,
    build_codegraph_impact_definition,
    build_codegraph_node_definition,
    build_codegraph_search_definition,
)
from app.tools.tool_models import (
    CodegraphCalleesArgs,
    CodegraphCallersArgs,
    CodegraphExploreArgs,
    CodegraphImpactArgs,
    CodegraphNodeArgs,
    CodegraphSearchArgs,
)


class _FakeClient:
    """记录 query 调用的假 client，可配置返回或抛错。"""

    def __init__(
        self,
        content: list[dict[str, Any]] | None = None,
        is_error: bool = False,
        raise_error: Exception | None = None,
    ) -> None:
        self._content = content or [{"type": "text", "text": "resolved source"}]
        self._is_error = is_error
        self._raise_error = raise_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def query(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if self._raise_error is not None:
            raise self._raise_error
        return QueryResult(content=self._content, is_error=self._is_error)


_ARGS_MODELS = {
    "codegraph_explore": CodegraphExploreArgs,
    "codegraph_search": CodegraphSearchArgs,
    "codegraph_node": CodegraphNodeArgs,
    "codegraph_callers": CodegraphCallersArgs,
    "codegraph_callees": CodegraphCalleesArgs,
    "codegraph_impact": CodegraphImpactArgs,
}


def _tool(name: str, client: _FakeClient | None = None) -> CodegraphQueryTool:
    return CodegraphQueryTool.for_tool(name, _ARGS_MODELS[name], "test description", client)


def _ec(workspace_root: str = "/ws/root") -> ToolExecutionContext:
    return ToolExecutionContext(
        task_id="task-1", workspace_id="ws-1", workspace_root=Path(workspace_root)
    )


def test_explore_injects_workspace_path_and_routes_method():
    client = _FakeClient()
    tool = _tool("codegraph_explore", client)
    obs = tool.execute(query="AuthService login", max_files=5, execution_context=_ec())
    assert obs.status == "success"
    method, params = client.calls[0]
    assert method == "codegraph_explore"
    assert params["workspace_path"] == str(Path("/ws/root"))
    assert params["query"] == "AuthService login"
    assert params["maxFiles"] == 5  # snake→camel


def test_node_maps_snake_to_camel():
    client = _FakeClient()
    tool = _tool("codegraph_node", client)
    tool.execute(symbol="foo", include_code=True, symbols_only=False, execution_context=_ec())
    method, params = client.calls[0]
    assert method == "codegraph_node"
    assert params["includeCode"] is True
    assert params["symbolsOnly"] is False


def test_success_aggregates_text_content():
    client = _FakeClient(
        content=[{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]
    )
    tool = _tool("codegraph_callers", client)
    obs = tool.execute(symbol="Foo", execution_context=_ec())
    assert obs.status == "success"
    assert "line1" in obs.content
    assert "line2" in obs.content


def test_kernel_unavailable_client_none_degrades():
    tool = _tool("codegraph_explore", client=None)
    obs = tool.execute(query="x", execution_context=_ec())
    assert obs.status == "error"
    assert "CodeGraph unavailable" in obs.error


def test_query_raise_degrades():
    client = _FakeClient(raise_error=CodeGraphKernelUnavailableError("kernel down"))
    tool = _tool("codegraph_impact", client)
    obs = tool.execute(symbol="Foo", execution_context=_ec())
    assert obs.status == "error"
    assert "failed" in obs.error.lower()


def test_no_workspace_degrades():
    tool = _tool("codegraph_explore", client=_FakeClient())
    obs = tool.execute(query="x", execution_context=None)
    assert obs.status == "error"
    assert "workspace" in obs.error


def test_all_factories_register_distinct_names():
    factories = [
        build_codegraph_explore_definition,
        build_codegraph_search_definition,
        build_codegraph_node_definition,
        build_codegraph_callers_definition,
        build_codegraph_callees_definition,
        build_codegraph_impact_definition,
    ]
    defs = [f() for f in factories]
    names = [d.name for d in defs]
    assert len(names) == 6
    assert len(set(names)) == 6
    assert all(d.permission == "codegraph_query" for d in defs)


_CODE_GRAPH_TOOL_NAMES = {
    "codegraph_explore",
    "codegraph_search",
    "codegraph_node",
    "codegraph_callers",
    "codegraph_callees",
    "codegraph_impact",
}


def test_build_tool_system_registers_codegraph_tools_with_client():
    """build_tool_system(client=fake) 后 registry 含 6 个 workspace_payload 工具。"""
    from app.tools.tool_system import ToolSystem

    ts = ToolSystem.build_tool_system(client=_FakeClient())  # type: ignore[arg-type]
    names = set(ts.registry.get_all_tool_names())
    assert _CODE_GRAPH_TOOL_NAMES.issubset(names)


def test_build_tool_system_none_client_registers_but_degrades():
    """build_tool_system(None) 工具仍注册、execute 降级（不抛）。"""
    from app.tools.tool_system import ToolSystem

    ts = ToolSystem.build_tool_system(client=None)
    names = set(ts.registry.get_all_tool_names())
    assert _CODE_GRAPH_TOOL_NAMES.issubset(names)
    # 取一个 workspace_payload 工具的 handler，execute 应降级为 error（client None）
    handler = ts.registry.get_tool_definition("codegraph_explore").handler
    obs = handler(query="x", execution_context=_ec())
    assert obs.status == "error"


def test_agent_profile_selects_codegraph_tools():
    """agent_profile.select_tools() 后 model_tools 含 6 个 workspace_payload 工具。"""
    from app.tools.tool_system import ToolSystem

    ts = ToolSystem.build_tool_system(client=_FakeClient())  # type: ignore[arg-type]
    from app.core.agents.agent_profile import DEFAULT_DEVELOPER_TOOLS, AgentProfile

    profile = AgentProfile(
        agent_id="developer",
        role="dev",
        goal="g",
        allowed_tools=list(DEFAULT_DEVELOPER_TOOLS),
        context_policy="default",
    )
    selected = profile.select_tools(ts.registry.get_all_definitions())
    selected_names = {t.name for t in selected}
    assert _CODE_GRAPH_TOOL_NAMES.issubset(selected_names)
