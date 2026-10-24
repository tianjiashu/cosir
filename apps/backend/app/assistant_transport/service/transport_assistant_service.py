"""Assistant Transport 的订阅适配：attach 只订阅 Task 当前 Run，不触发业务执行。"""

from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.service.transport_stream_service import (
    AssistantTransportStreamService,
)


class TransportAssistantService:
    """快照订阅与 SSE 编码委托 ``AssistantTransportStreamService``。"""

    def __init__(self) -> None:
        self._snapshots = ConversationTaskStateService()
        self._stream = AssistantTransportStreamService()

    def attach(self, task_id: int) -> StreamingResponse:
        """订阅 Task 当前 Run；提交与订阅之间发生正常交接时，订阅跟随交接后的当前 Run。

        异常:
            HTTPException: Task 还没有任何 Run 时返回 409 ``NO_RUN``。
        """

        run_id = self._snapshots.get_state(task_id)["current_run_id"]
        if run_id is None:
            _raise_transport_error(409, "NO_RUN", "当前对话还没有运行", retryable=False)
        response = StreamingResponse(
            self._stream.stream_envelopes(task_id, run_id, lambda: False),
            media_type="text/event-stream",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Connection"] = "keep-alive"
        response.headers["X-Cosir-Task-Id"] = str(task_id)
        return response


def _raise_transport_error(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool,
) -> NoReturn:
    """抛出统一的 Assistant Transport HTTP 错误；``message`` 是面向用户的安全提示。"""

    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "retryable": retryable}},
    )
