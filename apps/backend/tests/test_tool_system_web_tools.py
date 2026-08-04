"""ToolSystem 网页工具注册与调度集成测试。"""

import json
from pathlib import Path

from app.tools.schemas import ToolCall, ToolExecutionContext
from app.tools.tool_execute.tool_scheduler import ToolScheduler
from app.tools.tool_handler.web.web_provider import WebSearchItem
from app.tools.tool_handler.web.web_provider_registry import WebProviderRegistry
from app.tools.tool_handler.web_extract import (
    WebExtractItem,
    WebExtractTool,
)
from app.tools.tool_handler.web_search import (
    WebSearchTool,
    build_web_search_definition,
)
from app.tools.tool_registry import ToolRegistry
from app.tools.tool_system import ToolSystem


def test_tool_system_registers_web_search() -> None:
    """验证 ToolSystem 显式注册模型可见的网页搜索工具。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 注册表未包含 web_search 或 web_extract 时抛出。

    副作用:
        创建进程内工具注册表和调度器。
    """

    system = ToolSystem.build_tool_system()

    assert system.registry.get_tool_definition("web_search") is not None
    assert system.registry.get_tool_definition("web_extract") is not None


class _FakeSearchProvider:
    """测试用搜索 Provider，跳过真实网络调用。"""

    name = "exa"
    display_name = "Fake"

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def missing_configuration_message(self) -> str:
        return "Fake is not configured"

    def search(self, query: str, limit: int) -> list[WebSearchItem]:
        return [WebSearchItem("Title", "https://example.com", "Desc", 1)]


class _FakeExtractProvider:
    """测试用提取 Provider，跳过真实网络调用。"""

    name = "exa"
    display_name = "Fake"

    def is_available(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    def supported_extract_formats(self) -> frozenset[str]:
        return frozenset({"markdown", "html", "text"})

    def missing_configuration_message(self) -> str:
        return "Fake is not configured"

    def extract(self, urls: list[str], output_format: str, char_limit: int):
        return [
            WebExtractItem(
                url=urls[0],
                title="Page",
                content="Hello world",
                raw_content="Hello world",
                metadata={"source": "fake"},
            )
        ]


def _build_fake_search_registry() -> WebProviderRegistry:
    """构造注册了 fake 搜索 Provider 的注册表。"""
    registry = WebProviderRegistry()
    registry.register(_FakeSearchProvider())
    return registry


def _build_fake_extract_registry() -> WebProviderRegistry:
    """构造注册了 fake 提取 Provider 的注册表。"""
    registry = WebProviderRegistry()
    registry.register(_FakeExtractProvider())
    return registry


def test_web_search_executes_through_scheduler(tmp_path: Path) -> None:
    """验证 web_search 经 ToolScheduler 调度路径执行且不发起真实网络请求。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        AssertionError: 调度结果与权限断言不符时抛出。

    副作用:
        创建临时注册表与工具定义；不写入磁盘也不发起网络请求。
    """

    definition = build_web_search_definition(provider_registry=_build_fake_search_registry())
    scheduler = ToolScheduler(ToolRegistry([definition]))
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = scheduler.execute(
        ToolCall(tool_name="web_search", arguments={"query": "python", "limit": 1}),
        execution_context=context,
    )

    assert observation.tool_name == "web_search"
    assert observation.status == "success"
    assert observation.permission == "network"
    payload = json.loads(observation.content)
    assert payload["success"] is True
    assert payload["data"]["web"][0]["url"] == "https://example.com"


def test_web_extract_executes_through_scheduler(tmp_path: Path) -> None:
    """验证 web_extract 经 ToolScheduler 调度路径执行且不发起真实网络请求。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        AssertionError: 调度结果与权限断言不符时抛出。

    副作用:
        创建临时注册表与工具定义；不写入磁盘也不发起网络请求。
    """

    definition = WebExtractTool(
        provider_registry=_build_fake_extract_registry(),
        resolver=lambda host: ["93.184.216.34"],
    ).to_definition()
    scheduler = ToolScheduler(ToolRegistry([definition]))
    context = ToolExecutionContext("task_1", "workspace_1", tmp_path)

    observation = scheduler.execute(
        ToolCall(
            tool_name="web_extract",
            arguments={"urls": ["https://example.com/page"]},
        ),
        execution_context=context,
    )

    assert observation.tool_name == "web_extract"
    assert observation.status == "success"
    assert observation.permission == "network"
    payload = json.loads(observation.content)
    assert payload["success"] is True
    assert payload["results"][0]["content"] == "Hello world"


def test_web_search_carries_no_backend_rendering(tmp_path: Path) -> None:
    """验证 web_search 后端不再承载渲染：工具无 render_* 方法，display_data 为纯数据。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        AssertionError: 后端仍残留渲染方法或 display_data 混入渲染文本时抛出。

    副作用:
        无。
    """

    del tmp_path
    tool = WebSearchTool(_build_fake_search_registry())

    # 后端渲染方法必须不存在（渲染完全由客户端共享层负责）。
    assert not hasattr(tool, "render_request_summary")
    assert not hasattr(tool, "render_result_summary")

    definition = tool.to_definition()
    # display 仅为静态声明，不含摘要文本字段。
    assert definition.display is not None
    assert not hasattr(definition.display, "summary")


def test_web_extract_carries_no_backend_rendering(tmp_path: Path) -> None:
    """验证 web_extract 后端不再承载渲染：工具无 render_* 方法，display_data 为纯数据。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        无。

    异常:
        AssertionError: 后端仍残留渲染方法或 display_data 混入渲染文本时抛出。

    副作用:
        无。
    """

    del tmp_path
    tool = WebExtractTool(_build_fake_extract_registry())

    # 后端渲染方法必须不存在（渲染完全由客户端共享层负责）。
    assert not hasattr(tool, "render_request_summary")
    assert not hasattr(tool, "render_result_summary")

    definition = tool.to_definition()
    assert definition.display is not None
    assert not hasattr(definition.display, "summary")
