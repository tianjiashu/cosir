"""workspace .fileignore 配置响应结构。"""

from pydantic import BaseModel

from app.service.configuration.workspace_fileignore_configuration_service import (
    WorkspaceFileIgnoreDocument,
)


class WorkspaceFileIgnoreResponse(BaseModel):
    """序列化 workspace 忽略规则正文及规则数量。"""

    content: str
    path: str
    exists: bool
    rule_count: int
    max_rules: int

    @staticmethod
    def from_document(document: WorkspaceFileIgnoreDocument) -> "WorkspaceFileIgnoreResponse":
        """把 .fileignore 文档投影为 HTTP 响应。"""

        return WorkspaceFileIgnoreResponse(
            content=document.content,
            path=str(document.path),
            exists=document.exists,
            rule_count=document.rule_count,
            max_rules=document.max_rules,
        )
