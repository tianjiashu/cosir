"""``mcp`` 工具的 UI 展示数据构造。"""

from typing import Any


def build_mcp_call_display_data(
    *, server_name: str, tool_name: str, scope: str, images: list[str]
) -> dict[str, Any]:
    """``call``：调用了哪个服务的哪个工具，``scope`` 为服务所在的作用域（system / workspace），
    ``images`` 为结果里交给模型的图片（附件 asset id，卡片据此显示缩略图）。"""

    return {
        "kind": "mcp-tool-result",
        "action": "call",
        "server_name": server_name,
        "tool_name": tool_name,
        "scope": scope,
        "images": images,
    }


def build_mcp_catalog_display_data(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """发现结果：每个服务一项（``server_name`` 与 ``tools``，失败的服务带 ``error``）。

    卡片只列工具名与描述，输入 schema 只给模型：展示数据随快照持久化，原样带上 schema 会让每次
    发现都把全部工具的 schema 存一份，界面却用不上。
    """

    servers = [
        {
            **{key: value for key, value in server.items() if key != "tools"},
            "tools": [
                {"name": tool["name"], "description": tool["description"]}
                for tool in server["tools"]
            ],
        }
        for server in catalog
    ]
    return {"kind": "mcp-tool-result", "action": "list", "servers": servers}
