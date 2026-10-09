"""系统级终端 deny-list 配置 API。"""

from __future__ import annotations

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.request.TerminalDenylistUpdateRequest import (
    TerminalDenylistUpdateRequest,
)
from app.api.schemas.response.TerminalDenylistResponse import TerminalDenylistResponse
from app.app import app
from app.service.configuration.terminal_denylist_configuration_service import (
    TerminalDenylistConfigurationService,
)


@app.get("/configuration/terminal-denylist", response_model=TerminalDenylistResponse)
async def get_terminal_denylist_configuration() -> TerminalDenylistResponse:
    """读取 deny-list；缺失时由 service 初始化默认配置文件。"""

    try:
        document = TerminalDenylistConfigurationService().read()
        return TerminalDenylistResponse(patterns=list(document.patterns))
    except Exception as exc:
        raise_configuration_error(exc)


@app.put("/configuration/terminal-denylist", response_model=TerminalDenylistResponse)
async def update_terminal_denylist_configuration(
    payload: TerminalDenylistUpdateRequest,
) -> TerminalDenylistResponse:
    """校验并保存新的 pattern 列表。"""

    try:
        document = TerminalDenylistConfigurationService().update(payload.patterns)
        return TerminalDenylistResponse(patterns=list(document.patterns))
    except Exception as exc:
        raise_configuration_error(exc)
