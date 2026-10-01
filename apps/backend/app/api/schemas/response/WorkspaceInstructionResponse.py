"""workspace AGENTS.md 配置响应结构。"""

from pydantic import BaseModel

from app.service.configuration.workspace_instruction_configuration_service import (
    WorkspaceInstructionDocument,
)


class WorkspaceInstructionResponse(BaseModel):
    """序列化 workspace 当前有效指令文件及 token 预算。"""

    content: str
    path: str
    relative_path: str
    token_length: int
    max_tokens: int
    exists: bool
    effective_on: str = "next_run"

    @staticmethod
    def from_document(document: WorkspaceInstructionDocument) -> "WorkspaceInstructionResponse":
        """把 workspace 指令文档投影为 HTTP 响应。"""

        return WorkspaceInstructionResponse(
            content=document.content,
            path=str(document.path),
            relative_path=document.relative_path,
            token_length=document.token_length,
            max_tokens=document.max_tokens,
            exists=document.exists,
        )
