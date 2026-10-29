"""终端工作台 HTTP 响应体（camelCase wire）。"""

from pydantic import BaseModel, ConfigDict, Field


class TerminalShellsResponse(BaseModel):
    """当前宿主可用 shell；条目形状由终端服务产出（``kind``、``label``、``isDefault``、
    ``integrationSupported``）。"""

    shells: list[dict[str, object]]


class TerminalActiveCountResponse(BaseModel):
    """全进程活跃终端会话数，供桌面宿主退出前提醒。"""

    count: int


class CreateTerminalSessionResponse(BaseModel):
    """新建会话的身份；会话摘要随 snapshot ``terminal_sessions`` 推送。"""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(serialization_alias="sessionId")
