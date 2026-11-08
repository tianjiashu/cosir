"""主 Agent 系统提示词配置 HTTP 路由。"""

from __future__ import annotations

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.request.MainAgentPromptUpdateRequest import MainAgentPromptUpdateRequest
from app.api.schemas.response.MainAgentPromptResponse import MainAgentPromptResponse
from app.app import app
from app.config.configuration import replace_main_agent_profile
from app.service.configuration.main_agent_prompt_configuration_service import (
    MainAgentPromptConfigurationService,
)


@app.get("/configuration/main-agent-prompt", response_model=MainAgentPromptResponse)
async def get_main_agent_prompt_configuration() -> MainAgentPromptResponse:
    """读取主 Agent prompt，并返回当前有效正文。"""

    try:
        return MainAgentPromptResponse.from_document(MainAgentPromptConfigurationService().read())
    except Exception as exc:
        raise_configuration_error(exc)


@app.put("/configuration/main-agent-prompt", response_model=MainAgentPromptResponse)
async def update_main_agent_prompt_configuration(
    payload: MainAgentPromptUpdateRequest,
) -> MainAgentPromptResponse:
    """保存主 Agent prompt，并同步替换当前进程 Registry 中的 profile。"""

    try:
        document = MainAgentPromptConfigurationService().update(payload.content)
        replace_main_agent_profile(system_prompt=document.content)
        return MainAgentPromptResponse.from_document(document)
    except Exception as exc:
        raise_configuration_error(exc)
