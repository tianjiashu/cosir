"""CodeGraph 结构化展示的端到端集成测试（真实 Kernel 子进程）。

与 ``test_codegraph_result_parser.py`` 的分工：
- 单元测试用固定 fixture 锁定解析规则，快速、无外部依赖；
- 本集成测试拉起**真实 Kernel 子进程**，对**本仓库真实索引**发起查询，验证
  「vendor 真实输出 → handler → data['codegraph'] 结构化」整条链路成立，
  防止 fixture 与 vendor 真实格式漂移（单测全绿但线上全降级）。

需要真实 Kernel 与 node 运行时，环境不满足时整体 skip，不阻塞常规 CI：
- ``third_party/codegraph/dist/agent-kernel/server.js`` 存在；
- node 可解析（``CODING_AGENT_CODEGRAPH_NODE`` 或约定路径）。
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.codegraph.exceptions import CodeGraphKernelError
from app.codegraph.supervisor import CodeGraphKernelSupervisor
from app.tools.schemas.tool_execution_context import ToolExecutionContext
from app.tools.tool_handler.codegraph_query import CodegraphQueryTool
from app.tools.tool_models.codegraph_callers_args import CodegraphCallersArgs
from app.tools.tool_models.codegraph_explore_args import CodegraphExploreArgs
from app.tools.tool_models.codegraph_impact_args import CodegraphImpactArgs
from app.tools.tool_models.codegraph_search_args import CodegraphSearchArgs

PROJECT_ROOT = Path(__file__).resolve().parents[3]
KERNEL_ENTRY = PROJECT_ROOT / "third_party" / "codegraph" / "dist" / "agent-kernel" / "server.js"

pytestmark = pytest.mark.skipif(
    not KERNEL_ENTRY.exists(),
    reason="CodeGraph Kernel 未 vendor（third_party/codegraph/dist/agent-kernel/server.js 缺失）",
)


@pytest.fixture(scope="module")
def kernel_client() -> Iterator[Any]:
    """拉起真实 Kernel 子进程并提供 RPC client，模块结束后停止。

    返回:
        已就绪的 ``CodeGraphKernelClient``。

    异常:
        无。Kernel 启动失败（如本机缺 node 运行时）时 skip 整个模块。

    副作用:
        启动并在结束时终止一个 node 子进程。
    """
    supervisor = CodeGraphKernelSupervisor()
    try:
        supervisor.start()
    except CodeGraphKernelError as exc:
        pytest.skip(f"CodeGraph Kernel 无法启动（本机环境不满足）：{exc}")
    try:
        yield supervisor.get_client()
    finally:
        supervisor.shutdown()


@pytest.fixture(scope="module")
def execution_context() -> ToolExecutionContext:
    """构造指向本仓库根的工具执行上下文（Kernel 需要非空 workspace）。

    返回:
        ``ToolExecutionContext``，workspace_root 为本仓库根目录。

    异常:
        无。

    副作用:
        无。
    """
    return ToolExecutionContext(
        task_id="codegraph-integration-test",
        workspace_id="codegraph-integration-test",
        workspace_root=PROJECT_ROOT,
    )


def _run(client: Any, name: str, args_model: type, ctx: ToolExecutionContext, **params: Any):
    """按工具名构造 handler 并执行一次真实查询。

    参数:
        client: 已就绪的 Kernel client。
        name: 工具名。
        args_model: 该工具的参数模型。
        ctx: 工具执行上下文。
        params: 该工具的查询参数（snake_case）。

    返回:
        ``ToolObservation``。

    异常:
        无。

    副作用:
        发起一次真实 CodeGraph RPC。
    """
    tool = CodegraphQueryTool.for_tool(name, args_model, "integration test", client)
    return tool.execute(execution_context=ctx, **params)


def _assert_structured(observation: Any, tool_name: str) -> list[dict[str, Any]]:
    """断言观测结果已结构化，并返回条目列表。

    参数:
        observation: handler 返回的 ToolObservation。
        tool_name: 期望的工具名。

    返回:
        ``data['codegraph']['items']`` 列表。

    异常:
        AssertionError: 未成功、未结构化或条目为空时。

    副作用:
        无。
    """
    assert observation.status == "success", f"{tool_name} 查询失败: {observation.content}"
    assert observation.data is not None
    codegraph = observation.data["codegraph"]
    assert codegraph["tool"] == tool_name
    assert "items" in codegraph, (
        f"{tool_name} 未结构化，降级为全文（vendor 输出格式可能已变更）: "
        f"{codegraph.get('raw', '')[:300]}"
    )
    items = codegraph["items"]
    assert items, f"{tool_name} 结构化条目为空"
    return items


def test_search_end_to_end(kernel_client: Any, execution_context: ToolExecutionContext) -> None:
    """search 真实查询应产出带路径与行号的结构化条目，且 content 保持原文。"""
    observation = _run(
        kernel_client,
        "codegraph_search",
        CodegraphSearchArgs,
        execution_context,
        query="resolve_node_binary",
    )
    items = _assert_structured(observation, "codegraph_search")

    assert any(item["name"] == "resolve_node_binary" for item in items)
    for item in items:
        assert item["filePath"], f"条目缺少文件路径: {item}"
        assert isinstance(item["lineNumber"], int) and item["lineNumber"] > 0
    # 给模型的文本仍是 vendor 原文，未被结构化改写。
    assert "resolve_node_binary" in observation.content
    # 结构化只进 data，不污染 content。
    assert "filePath" not in observation.content


def test_callers_end_to_end(kernel_client: Any, execution_context: ToolExecutionContext) -> None:
    """callers 真实查询应产出结构化条目，并解析出 kind 字段。"""
    observation = _run(
        kernel_client,
        "codegraph_callers",
        CodegraphCallersArgs,
        execution_context,
        symbol="resolve_node_binary",
    )
    items = _assert_structured(observation, "codegraph_callers")

    assert any(item["kind"] for item in items), f"callers 未解析出任何 kind: {items}"
    for item in items:
        assert item["filePath"]
        assert isinstance(item["lineNumber"], int)


def test_impact_end_to_end(kernel_client: Any, execution_context: ToolExecutionContext) -> None:
    """impact 真实查询应按文件扁平化出带路径的受影响符号。"""
    observation = _run(
        kernel_client,
        "codegraph_impact",
        CodegraphImpactArgs,
        execution_context,
        symbol="resolve_node_binary",
    )
    items = _assert_structured(observation, "codegraph_impact")

    assert all(item["filePath"] for item in items)
    assert any(item["name"] == "resolve_node_binary" for item in items)


def test_explore_stays_raw(kernel_client: Any, execution_context: ToolExecutionContext) -> None:
    """explore 本期不结构化：真实查询应恒返回全文，不产出 items。"""
    observation = _run(
        kernel_client,
        "codegraph_explore",
        CodegraphExploreArgs,
        execution_context,
        query="CodeGraph Kernel 如何解析 node 运行时",
    )
    assert observation.status == "success", observation.content
    assert observation.data is not None
    codegraph = observation.data["codegraph"]
    assert "items" not in codegraph
    assert codegraph["raw"]
