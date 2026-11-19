"""MCP 服务连接测试与登录授权的响应。"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class MCPServerTestResponse(BaseModel):
    """一次连接测试（或登录授权）的结果：成功时带发现的工具，失败时带归类后的原因与 ``failure_kind``。"""

    status: Literal["completed"] = "completed"
    success: bool
    tools: list[dict[str, Any]]
    error: str | None = None
    failure_kind: str | None = None
    # 下一步是「登录授权」（由后端失败归类给出，前端不按 ``failure_kind`` 自行判断）。
    needs_sign_in: bool = False
    agent_available: bool = False


class MCPAuthorizationPendingResponse(BaseModel):
    """登录授权停在浏览器这一步：前端打开 ``authorization_url``，再按 ``flow_id`` 等最终结果。"""

    status: Literal["authorization_required"] = "authorization_required"
    flow_id: str
    authorization_url: str


# 「登录授权」可能直接得出结果（已有可用令牌、服务不要求授权），也可能要用户去浏览器：按 ``status`` 区分。
MCPServerAuthorizeResponse = Annotated[
    MCPServerTestResponse | MCPAuthorizationPendingResponse,
    Field(discriminator="status"),
]
