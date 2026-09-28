"""主 Agent 系统提示词响应结构。"""

from typing import Literal

from pydantic import BaseModel

from app.service.configuration.main_agent_prompt_configuration_service import (
    MainAgentPromptDocument,
)


class MainAgentPromptResponse(BaseModel):
    """把主 Agent prompt 配置文档投影为配置中心响应。"""

    content: str
    path: str
    token_length: int
    max_tokens: int
    source: Literal["user_file", "builtin_default", "builtin_fallback"]
    effective_on: Literal["next_run"] = "next_run"

    @staticmethod
    def from_document(document: MainAgentPromptDocument) -> "MainAgentPromptResponse":
        """把 service 文档转换为 HTTP 响应。"""

        return MainAgentPromptResponse(
            content=document.content,
            path=str(document.path),
            token_length=document.token_length,
            max_tokens=document.max_tokens,
            source=document.source,  # type: ignore[arg-type]
        )
