"""测试 MCP 服务连接的请求体。"""

from app.api.schemas.request.MCPServerConfigRequest import MCPServerConfigRequest


class MCPServerTestRequest(MCPServerConfigRequest):
    """描述一次未保存的 MCP 服务连接测试。"""
