import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_conversation_command_service,
    get_conversation_mutation_writer,
    get_conversation_run_executor,
    get_conversation_run_service,
    get_conversation_state_service,
    get_runtime,
    get_task_service,
    get_turn_service,
)
from app.app import app
from app.config.settings import Settings
from app.service import depends as service_depends
from app.service.task.conversation_mutation_writer import ConversationMutationWriter
from app.service.task.turn_service import TurnService
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.store_engines import init_storage

# 记录最近一次 create_turn 调用的透传字段，供用例断言。
# 之所以用模块级捕获而非实例属性：FastAPI dependency_overrides 的值必须是
# 可调用对象（FastAPI 会调用它取得依赖实例），故 override 使用 FakeTurnService
# 类本身；实例在每次请求时新建，实例属性无法跨「请求触发的新实例」被用例读取。
_last_create_turn: dict = {}
_fake_assistant_text = ""
_fake_revision = 0


class FakeConversationStateService:
    """不落库的初始 state 构造替身。

    集成测试用 ``FakeTurnService`` 返回 ``id=9`` 的占位 turn，但并未写入数据库；
    真实的 ``ConversationStateService.build_initial_state`` 会按 ``turn.id`` 回查数据库
    取本轮事实，此处无法命中。该替身只返回符合 ``build_initial_state`` 投影形状的
    初始 state，使测试聚焦在 transport 流与字段透传上（state 内容由
    ``test_conversation_state_service.py`` 单独验证）。

    ``build_initial_history_state`` 是首屏 GET 端点的投影入口，这里同样返回占位形状，
    并把 ``task_id`` 透传到消息文本，便于断言「端点正确转发了 task 的历史投影」。
    """

    def build_initial_state(self, task_id: int, current_turn_id: int, current_text: str) -> dict:
        return {
            "messages": [
                {
                    "id": f"turn-{current_turn_id}-user",
                    "role": "user",
                    "status": "completed",
                    "createdAt": "2024-01-01T00:00:00+00:00",
                    "parts": [{"type": "text", "text": current_text, "status": "completed"}],
                },
                {
                    "id": f"turn-{current_turn_id}-assistant",
                    "role": "assistant",
                    "status": "running",
                    "createdAt": "2024-01-01T00:00:00+00:00",
                    "parts": [{"type": "text", "text": "", "status": "running"}],
                },
            ],
            "run": {"runId": current_turn_id, "status": "pending"},
            "revision": 0,
        }

    def build_initial_history_state(self, task_id: int) -> dict:
        if task_id == 1 and _fake_assistant_text:
            return {
                "messages": [
                    {
                        "id": "message-9-user",
                        "role": "user",
                        "status": "completed",
                        "createdAt": "2024-01-01T00:00:00+00:00",
                        "parts": [{"type": "text", "text": "hello", "status": "completed"}],
                    },
                    {
                        "id": "message-10-assistant",
                        "role": "assistant",
                        "status": "completed",
                        "createdAt": "2024-01-01T00:00:00+00:00",
                        "parts": [
                            {"type": "text", "text": _fake_assistant_text, "status": "completed"}
                        ],
                    },
                ],
                "run": {"runId": 9, "status": "completed"},
                "revision": _fake_revision,
            }
        return {
            "messages": [
                {
                    "id": f"turn-{task_id}-history-user",
                    "role": "user",
                    "status": "completed",
                    "createdAt": "2024-01-01T00:00:00+00:00",
                    "parts": [
                        {"type": "text", "text": f"history-{task_id}", "status": "completed"}
                    ],
                },
            ],
            "run": {"runId": None, "status": "idle"},
            "revision": 0,
        }

    def build_run_state(self, task_id: int, run_id: int) -> dict:
        """返回订阅器使用的指定 run 占位快照。"""
        state = self.build_initial_history_state(task_id)
        if _fake_assistant_text:
            return state
        return self.build_initial_state(task_id, run_id, "hello")


class FakeConversationCommandService:
    """不落库的命令幂等替身。"""

    def payload_hash(self, payload):
        return "hash"

    def reserve_or_get(self, task_id, command_id, command_type, payload_hash):
        return SimpleNamespace(payload_hash=payload_hash, turn_id=None, id=1), True

    def bind_turn(self, command, turn_id):
        return None

    def mark_failed(self, command, error_code):
        return None

    def mark_status(self, command, status):
        return None


class FakeTaskService:
    """不落库的任务存在性校验替身。

    ``get_task`` 对 ``task_id == 1`` 返回占位任务，其余抛 ``KeyError``，
    以复刻真实 ``TaskService.get_task`` 的「不存在即 KeyError」约定。
    """

    def get_task(self, task_id: int) -> SimpleNamespace:
        if task_id != 1:
            raise KeyError(task_id)
        return SimpleNamespace(id=task_id)


class FakeTurnService:
    def create_turn(
        self,
        task_id: int,
        text: str,
        agent_id: str | None = None,
        status: str = "pending",
        provider_id: int | None = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        attachments=None,
        session=None,
    ) -> SimpleNamespace:
        assert task_id == 1
        assert text == "hello"
        assert agent_id == "main_agent"
        # 记录透传字段，供用例断言 B3 的字段确实被传递。
        _last_create_turn.clear()
        _last_create_turn.update(
            provider_id=provider_id,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )
        return SimpleNamespace(id=9, task_id=1)


class FakeConversationRunService:
    """不落库的 command + turn 原子启动替身。"""

    def start(
        self,
        task_id,
        command_id,
        command_type,
        payload_hash,
        input_text,
        provider_id=None,
        model_name=None,
        reasoning_effort=None,
    ):
        global _fake_assistant_text, _fake_revision
        _fake_assistant_text = ""
        _fake_revision = 0
        turn = FakeTurnService().create_turn(
            task_id,
            input_text,
            agent_id="main_agent",
            provider_id=provider_id,
            model_name=model_name,
            reasoning_effort=reasoning_effort,
        )
        return SimpleNamespace(
            command=SimpleNamespace(payload_hash=payload_hash, turn_id=turn.id, id=1),
            turn=turn,
        )


class FakeConversationMutationWriter:
    """Transport 单测的事实写入替身；本文件的运行时使用内存占位 task。"""

    def append_assistant_text_for_turn(self, task_id, turn_id, text, fencing_version=None):
        global _fake_assistant_text, _fake_revision
        _fake_assistant_text += text
        _fake_revision += 1
        return None

    def finish_assistant_for_turn(
        self, task_id, turn_id, status, end_reason=None, fencing_version=None
    ):
        return None

    def settle_run(self, run_id, status, end_reason=None, fencing_version=None):
        return None


class FakeConversationRunExecutor:
    """Transport 单测的后台执行器替身。"""

    def __init__(self):
        self.task = None

    async def start(self, run_id, runner):
        self.task = asyncio.create_task(runner(SimpleNamespace(id=run_id, task_id=1)))
        return self.task

    async def status(self, run_id):
        if self.task is None:
            return None
        return SimpleNamespace(status="completed" if self.task.done() else "running")


class FakeRuntime:
    """提供只提交 canonical facts 的 runtime 替身。"""

    async def run_turn(self, turn):
        global _fake_assistant_text, _fake_revision
        assert turn.id == 9
        _fake_assistant_text = "world"
        _fake_revision = 1


def test_assistant_transport_streams_state_updates() -> None:
    app.dependency_overrides[get_runtime] = FakeRuntime
    app.dependency_overrides[get_turn_service] = FakeTurnService
    app.dependency_overrides[get_conversation_state_service] = FakeConversationStateService
    app.dependency_overrides[get_conversation_command_service] = FakeConversationCommandService
    app.dependency_overrides[get_conversation_run_service] = FakeConversationRunService
    app.dependency_overrides[get_conversation_mutation_writer] = FakeConversationMutationWriter
    app.dependency_overrides[get_conversation_run_executor] = FakeConversationRunExecutor
    try:
        with TestClient(app) as client:
            # 使用前端真实路径（B1 改后的契约），避免锁死旧的 /api 前缀。
            response = client.post(
                "/assistant",
                json={
                    "taskId": 1,
                    "threadId": "task-1",
                    "commands": [
                        {
                            "type": "add-message",
                            "commandId": "cmd-1",
                            "message": {
                                "role": "user",
                                "parts": [{"type": "text", "text": "hello"}],
                            },
                        }
                    ],
                },
            )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "update-state" in response.text
        assert "world" in response.text
        assert "[DONE]" in response.text
        # 字段缺失时保持默认行为：不向 create_turn 透传模型选择。
        assert _last_create_turn["provider_id"] is None
        assert _last_create_turn["model_name"] is None
        assert _last_create_turn["reasoning_effort"] is None
    finally:
        app.dependency_overrides.clear()


def test_assistant_transport_passes_model_selection() -> None:
    app.dependency_overrides[get_runtime] = FakeRuntime
    app.dependency_overrides[get_turn_service] = FakeTurnService
    app.dependency_overrides[get_conversation_state_service] = FakeConversationStateService
    app.dependency_overrides[get_conversation_command_service] = FakeConversationCommandService
    app.dependency_overrides[get_conversation_run_service] = FakeConversationRunService
    app.dependency_overrides[get_conversation_mutation_writer] = FakeConversationMutationWriter
    app.dependency_overrides[get_conversation_run_executor] = FakeConversationRunExecutor
    try:
        with TestClient(app) as client:
            response = client.post(
                "/assistant",
                json={
                    "taskId": 1,
                    "threadId": "task-1",
                    "commands": [
                        {
                            "type": "add-message",
                            "commandId": "cmd-1",
                            "message": {
                                "role": "user",
                                "parts": [{"type": "text", "text": "hello"}],
                            },
                        }
                    ],
                    "providerId": 2,
                    "modelName": "gpt-4o",
                    "reasoningEffort": "high",
                },
            )
        assert response.status_code == 200
        assert "update-state" in response.text
        assert "world" in response.text
        # 三个字段必须正确透传到 create_turn。
        assert _last_create_turn["provider_id"] == 2
        assert _last_create_turn["model_name"] == "gpt-4o"
        assert _last_create_turn["reasoning_effort"] == "high"
    finally:
        app.dependency_overrides.clear()


def test_assistant_transport_rejects_invalid_reasoning_effort() -> None:
    app.dependency_overrides[get_runtime] = FakeRuntime
    app.dependency_overrides[get_turn_service] = FakeTurnService
    app.dependency_overrides[get_conversation_state_service] = FakeConversationStateService
    app.dependency_overrides[get_conversation_command_service] = FakeConversationCommandService
    app.dependency_overrides[get_conversation_run_service] = FakeConversationRunService
    app.dependency_overrides[get_conversation_mutation_writer] = FakeConversationMutationWriter
    app.dependency_overrides[get_conversation_run_executor] = FakeConversationRunExecutor
    try:
        with TestClient(app) as client:
            response = client.post(
                "/assistant",
                json={
                    "taskId": 1,
                    "threadId": "task-1",
                    "commands": [
                        {
                            "type": "add-message",
                            "commandId": "cmd-1",
                            "message": {
                                "role": "user",
                                "parts": [{"type": "text", "text": "hello"}],
                            },
                        }
                    ],
                    "reasoningEffort": "ultra",
                },
            )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "REASONING_EFFORT_INVALID"
        assert error["message"]
        assert error["retryable"] is False
    finally:
        app.dependency_overrides.clear()


def test_assistant_state_returns_history_for_existing_task() -> None:
    global _fake_assistant_text, _fake_revision
    _fake_assistant_text = ""
    _fake_revision = 0
    app.dependency_overrides[get_task_service] = FakeTaskService
    app.dependency_overrides[get_conversation_state_service] = FakeConversationStateService
    try:
        with TestClient(app) as client:
            response = client.get("/tasks/1/assistant/state")
        assert response.status_code == 200
        body = response.json()
        # 形状与 POST 端点一致：含 messages / run / revision。
        assert "messages" in body
        assert body["run"]["status"] == "idle"
        assert body["revision"] == 0
        # 端点把 task 历史投影透传出来。
        assert body["messages"][0]["parts"][0]["text"] == "history-1"
    finally:
        app.dependency_overrides.clear()


def test_assistant_state_empty_task_returns_empty_messages() -> None:
    app.dependency_overrides[get_task_service] = FakeTaskService
    app.dependency_overrides[get_conversation_state_service] = FakeEmptyStateService
    try:
        with TestClient(app) as client:
            response = client.get("/tasks/1/assistant/state")
        assert response.status_code == 200
        assert response.json()["messages"] == []
    finally:
        app.dependency_overrides.clear()


def test_assistant_state_missing_task_returns_404() -> None:
    app.dependency_overrides[get_task_service] = FakeTaskService
    app.dependency_overrides[get_conversation_state_service] = FakeConversationStateService
    try:
        with TestClient(app) as client:
            # task_id 非 1 → FakeTaskService 抛 KeyError → 应映射为 404。
            response = client.get("/tasks/999/assistant/state")
        assert response.status_code == 404
        assert response.json()["detail"] == "task not found"
    finally:
        app.dependency_overrides.clear()


class FakeEmptyStateService:
    """返回空历史的投影替身，用于验证「空 task → 空 messages」。"""

    def build_initial_history_state(self, task_id: int) -> dict:
        return {"messages": [], "run": {"runId": None, "status": "idle"}, "revision": 0}


# ---------------------------------------------------------------------------
# 取消端点（T5）集成测试：基于真实 SQLite 临时库，验证「薄端点复用 service」的
# 成功/不存在/已终态三种语义映射。与上方 Fake 用例隔离，不污染 Fake override。
#
# 注意：``TestClient`` 进入时会触发应用 ``lifespan``，后者用默认 ``Settings`` 路径
# 重新 ``init_storage`` 并缓存 service 单例，覆盖本模块在 ``with`` 之前的任何存储设置。
# 因此存储重定向必须在 ``with TestClient`` **内部**完成——先经 lifespan 默认库 init，
# 再由 ``_redirect_storage_to_temp`` 覆写为临时库并清空 service 缓存，使端点解析出的
# ``TurnService`` 指向临时库；所有断言也必须在 ``with`` 内完成（shutdown 会 ``close_storage``）。
# ---------------------------------------------------------------------------

_cancel_workspace_id = 0
_cancel_task_id = 0


def _redirect_storage_to_temp() -> None:
    """在 ``with TestClient`` 内部把存储重定向到临时 SQLite 并重置 service 缓存。

    参数:
        无。

    返回:
        无。

    异常:
        OSError: 如果临时目录或数据库文件无法创建。

    副作用:
        覆写 ``Settings`` 数据库路径并重新初始化存储引擎；清空 service 层 lru_cache
        单例，使端点依赖解析出的 ``TurnService``/``TurnCrud`` 重建到临时库（否则沿用
        lifespan 在默认库创建的缓存实例）。
    """
    service_depends.reset_service_dependencies()
    tmp = Path(tempfile.mkdtemp(prefix="cosir-cancel-"))
    Settings.override(
        LOG_DIR=tmp / "logs",
        DATABASE_FILE=tmp / "app.sqlite3",
        LOG_DATABASE_FILE=tmp / "logs.sqlite3",
        CHECKPOINT_FILE=tmp / "langgraph_checkpoints.sqlite",
    )
    init_storage()


def _make_cancel_task() -> None:
    """创建隔离的 workspace 与 task，结果写入模块级变量。

    参数:
        无。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

    副作用:
        向 workspaces / tasks 表各插入一行。
    """
    global _cancel_workspace_id, _cancel_task_id
    ws = WorkspaceCrud().create("cancel-ws", str(Path(tempfile.mkdtemp())))
    _cancel_workspace_id = ws.id
    task = TaskCrud().create(_cancel_workspace_id, "cancel-task")
    _cancel_task_id = task.id


def _create_cancel_turn(input_text: str, status: str) -> int:
    """在隔离 task 下创建一条指定状态的真实 turn。

    参数:
        input_text: 用户提问文本。
        status: 轮次状态字符串（如 ``pending`` / ``running`` / ``completed``）。

    返回:
        新创建 turn 的整数标识。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

    副作用:
        向 turns 表插入一行（先 pending，再原子更新为目标状态）。
    """
    turn_id = TurnCrud().create(_cancel_task_id, input_text, status="pending").id
    TurnCrud().update_status_if_in(
        turn_id,
        target_status=status,
        allowed_statuses=("pending", "running", "completed", "failed", "cancelled"),
    )
    ConversationMutationWriter().create_message(
        _cancel_task_id,
        "assistant",
        turn_id=turn_id,
        status="running" if status in {"pending", "running"} else status,
        text="",
    )
    return turn_id


def test_cancel_run_success_running() -> None:
    """E1/T5：running turn 经取消端点落定为 cancelled（200）。"""
    with TestClient(app) as client:
        _redirect_storage_to_temp()
        _make_cancel_task()
        turn_id = _create_cancel_turn("hi", status="running")
        response = client.post(f"/runs/{turn_id}/cancel")
        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == turn_id
        assert body["status"] == "cancelled"
        # 后端事实已落定：service 读取确为 cancelled。
        assert TurnService().get_turn(turn_id).status == "cancelled"


def test_cancel_run_success_pending() -> None:
    """E1/T5：pending turn 同样可取消（200）。"""
    with TestClient(app) as client:
        _redirect_storage_to_temp()
        _make_cancel_task()
        turn_id = _create_cancel_turn("hi", status="pending")
        response = client.post(f"/runs/{turn_id}/cancel")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"


def test_cancel_run_missing_returns_404() -> None:
    """E1/T5：turn 不存在 → 404（复用 KeyError 约定）。"""
    with TestClient(app) as client:
        _redirect_storage_to_temp()
        _make_cancel_task()
        # 该 turn 从未创建，底层 get 抛 KeyError → 映射为 404。
        response = client.post("/runs/999999/cancel")
        assert response.status_code == 404
        assert response.json()["detail"] == "run not found"


def test_cancel_run_terminal_state_returns_409() -> None:
    """E1/T5：已终态（completed）turn 不允许取消 → 409（与资源当前状态冲突）。"""
    with TestClient(app) as client:
        _redirect_storage_to_temp()
        _make_cancel_task()
        turn_id = _create_cancel_turn("hi", status="completed")
        response = client.post(f"/runs/{turn_id}/cancel")
        assert response.status_code == 409
        assert response.json()["detail"] == "run is not in a cancellable state"
        # 终态未被改写。
        assert TurnService().get_turn(turn_id).status == "completed"
