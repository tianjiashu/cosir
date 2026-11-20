"""MCP 工具调用的 UI 展示数据构造。"""

from typing import Any


def build_mcp_call_display_data(
    *, server_name: str, tool_name: str, scope: str, images: list[str]
) -> dict[str, Any]:
    """调用了哪个服务的哪个工具（原名，模型可见名在调用的工具名上），``scope`` 为服务所在的作用域
    （system / workspace），``images`` 为结果里交给模型的图片（附件 asset id，卡片据此显示缩略图）。"""

    return {
        "kind": "mcp-tool-result",
        "server_name": server_name,
        "tool_name": tool_name,
        "scope": scope,
        "images": images,
    }
