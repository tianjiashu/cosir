"""API 请求/响应 Pydantic 模型包。

每个模型独立成文件（单一职责），本模块集中重导出对外公开的模型类，
使 ``from app.api.schemas import CreateTaskRequest`` 解析到类本身，
而非同名子模块（空 ``__init__`` 会让 ``import`` 拿到子模块导致
FastAPI/Pydantic 把模块当作类型注解从而报错）。
"""

from app.api.schemas.CreateTaskRequest import CreateTaskRequest
from app.api.schemas.CreateTurnRequest import CreateTurnRequest
from app.api.schemas.CreateWorkspaceRequest import CreateWorkspaceRequest

__all__ = [
    "CreateTaskRequest",
    "CreateTurnRequest",
    "CreateWorkspaceRequest",
]
