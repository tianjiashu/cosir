"""ToolSystem 网页工具注册测试。"""

from app.tools.tool_system import ToolSystem


def test_tool_system_registers_web_search() -> None:
    """验证 ToolSystem 显式注册模型可见的网页搜索工具。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 注册表未包含 web_search 时抛出。

    副作用:
        创建进程内工具注册表和调度器。
    """

    system = ToolSystem.build_tool_system()

    assert system.registry.get_tool_definition("web_search") is not None
    assert system.registry.get_tool_definition("web_extract") is not None
