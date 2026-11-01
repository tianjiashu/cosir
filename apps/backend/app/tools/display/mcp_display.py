"""``mcp`` 工具的 UI 展示数据构造。"""

from typing import Any


def build_mcp_call_display_data(*, server_name: str, tool_name: str) -> dict[str, Any]:
    return {
        "kind": "mcp-tool-result",
        "action": "call",
        "server_name": server_name,
        "tool_name": tool_name,
    }


def build_mcp_catalog_display_data(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """发现结果：每个服务一项（``server_name`` 与 ``tools``，失败的服务带 ``error``）。"""

    return {"kind": "mcp-tool-result", "action": "list", "servers": catalog}
