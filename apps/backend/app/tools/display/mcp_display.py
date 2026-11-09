"""``mcp`` 工具的 UI 展示数据构造。"""

from typing import Any


def build_mcp_call_display_data(*, server_name: str, tool_name: str, scope: str) -> dict[str, Any]:
    """``call``：调用了哪个服务的哪个工具，``scope`` 为服务所在的作用域（system / workspace）。"""

    return {
        "kind": "mcp-tool-result",
        "action": "call",
        "server_name": server_name,
        "tool_name": tool_name,
        "scope": scope,
    }


def build_mcp_catalog_display_data(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """发现结果：每个服务一项（``server_name`` 与 ``tools``，失败的服务带 ``error``）。"""

    return {"kind": "mcp-tool-result", "action": "list", "servers": catalog}
