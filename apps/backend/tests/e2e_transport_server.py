"""用于桌面端 Playwright 的真实 FastAPI Transport 测试服务器。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.dependencies import (
    get_conversation_run_executor,
    get_conversation_run_service,
    get_conversation_state_service,
    get_runtime,
)
from tests.test_assistant_transport import (
    FakeConversationRunExecutor,
    FakeConversationRunService,
    FakeConversationStateService,
    FakeRuntime,
)
from app.app import app
import uvicorn


app.dependency_overrides[get_runtime] = FakeRuntime
app.dependency_overrides[get_conversation_state_service] = FakeConversationStateService
app.dependency_overrides[get_conversation_run_service] = FakeConversationRunService
app.dependency_overrides[get_conversation_run_executor] = FakeConversationRunExecutor


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
