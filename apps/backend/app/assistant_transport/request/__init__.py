"""Assistant Transport 请求的 wire schema。"""

from app.assistant_transport.request.task_command_request import (
    DecisionRequest,
    SendMessageRequest,
)

__all__ = ["DecisionRequest", "SendMessageRequest"]
