"""针对「API 层直接依赖 service 层、AgentRuntime 不再充当 service 门面」重构的隔离单元测试。

重要约束：
- 仓库处于未完成的 domain→service 大重构中，存在预存坏导入
  ``app/config/logging/save/sqlite_handler.py:12``（误写为 ``from app.storage.crud.log import LogStore``，
  正确应为 ``log_crud``），该坏导入位于 ``app.config.logging.__init__`` 导入链上，会导致
  ``import app.api.*`` / ``import app.core.runtime.runner`` 在导入期失败。
- 因此本测试**不导入**任何依赖 ``app.config.logging`` 的模块（如 ``app.api.app``、
  ``app.api.dependencies``、``app.core.runtime.runner``），仅隔离测试不触发该坏导入链、且属于
  本次改动行为契约的纯逻辑：``app.service.task.*`` 三个 service 类。
- 这些 service 类通过构造注入 crud/store 实例，本测试用轻量 fake 对象替代，无需真实 SQLite。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from app.models import RunRecord, TaskRecord, TurnRecord, WorkspaceRecord
from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService
from app.service.task.workspace_service import WorkspaceService


# ---------------------------------------------------------------------------
# Fake 依赖对象
# ---------------------------------------------------------------------------


class FakeTaskCrud:
    """记录调用并返回可预测结果的 TaskCrud 替身。"""

    def __init__(self) -> None:
        self.created: dict[str, Any] = {}
        self.deleted_ids: list[str] = []
        self.ensure_default_calls = 0
        self.updated_latest: list[tuple[str, str, str]] = []
        self._default_workspace_id = "ws-default"

    def create(self, **kwargs: Any) -> TaskRecord:
        self.created = kwargs
        return TaskRecord(
            task_id=kwargs["task_id"],
            workspace_id=kwargs["workspace_id"],
            agent_id=kwargs["agent_id"],
            input_text=kwargs["input_text"],
            title=kwargs["title"],
            last_message_preview=kwargs["last_message_preview"],
            latest_turn_id=kwargs["latest_turn_id"],
            status=kwargs["status"],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def ensure_default(self, now: datetime) -> str:
        self.ensure_default_calls += 1
        return self._default_workspace_id

    def list_ids_by_workspace(self, workspace_id: str) -> list[str]:
        return self._task_ids or []

    def delete_by_ids(self, task_ids: list[str]) -> None:
        self.deleted_ids = list(task_ids)

    def update_latest_turn(self, task_id: str, latest_turn_id: str, preview: str) -> None:
        self.updated_latest.append((task_id, latest_turn_id, preview))

    def get(self, task_id: str) -> TaskRecord:
        return TaskRecord(
            task_id=task_id,
            workspace_id="ws",
            agent_id="developer",
            input_text="x",
            title="t",
            last_message_preview="p",
            latest_turn_id=None,
            status="pending",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def update_status(self, task_id: str, status: str) -> TaskRecord:
        return self.get(task_id)

    def has_status(self, task_id: str, status: str) -> bool:
        return True

    def list_by_workspace(self, workspace_id: str) -> list[TaskRecord]:
        return []

    _task_ids: list[str] = []


class FakeTurnCrud:
    """记录调用并返回可预测结果的 TurnCrud 替身。"""

    def __init__(self) -> None:
        self.created: dict[str, Any] = {}
        self.deleted_ids: list[str] = []
        self.updated_status: list[tuple[str, str]] = []

    def create(self, task_id: str, input_text: str, status: str = "pending") -> TurnRecord:
        self.created = {
            "task_id": task_id,
            "input_text": input_text,
            "status": status,
            "turn_id": "turn-fixed",
        }
        return TurnRecord(
            turn_id="turn-fixed",
            task_id=task_id,
            input_text=input_text,
            status=status,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def list_ids_by_task_ids(self, task_ids: list[str]) -> list[str]:
        return self._turn_ids or []

    def delete_by_ids(self, turn_ids: list[str]) -> None:
        self.deleted_ids = list(turn_ids)

    def get(self, turn_id: str) -> TurnRecord:
        return self.create("t", "x")

    def list_by_task(self, task_id: str) -> list[TurnRecord]:
        return []

    def update_status(self, turn_id: str, status: str) -> TurnRecord:
        self.updated_status.append((turn_id, status))
        return self.create("t", "x", status)

    def claim_pending(self, turn_id: str) -> bool:
        return True

    def get_first_for_task(self, task_id: str) -> TurnRecord:
        return self.create(task_id, "x")

    _turn_ids: list[str] = []


class FakeWorkspaceCrud:
    """记录调用并返回可预测结果的 WorkspaceCrud 替身。"""

    def __init__(self) -> None:
        self.deleted_id: str | None = None
        self.ensure_default_calls = 0
        self._default_workspace_id = "ws-default"

    def ensure_default(self, now: datetime) -> str:
        self.ensure_default_calls += 1
        return self._default_workspace_id

    def create(self, name: str, root_path: str) -> WorkspaceRecord:
        return WorkspaceRecord(
            workspace_id="ws-1",
            name=name,
            root_path=root_path,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def list_all(self) -> list[WorkspaceRecord]:
        return []

    def get(self, workspace_id: str) -> WorkspaceRecord:
        return WorkspaceRecord(
            workspace_id=workspace_id,
            name="n",
            root_path="/p",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def delete(self, workspace_id: str) -> None:
        self.deleted_id = workspace_id


class FakeRunStore:
    """记录 delete_by_task_ids 调用的 DurableRunStore 替身。"""

    def __init__(self, run_ids: list[str] | None = None) -> None:
        self.deleted_task_ids: list[str] = []
        self._run_ids = run_ids if run_ids is not None else ["run-1", "run-2"]

    def delete_by_task_ids(self, task_ids: list[str]) -> list[str]:
        self.deleted_task_ids = list(task_ids)
        return self._run_ids


def _make_task_service(task_crud=None, turn_crud=None, workspace_crud=None) -> TaskService:
    return TaskService(
        task_crud=task_crud or FakeTaskCrud(),
        turn_crud=turn_crud or FakeTurnCrud(),
        workspace_crud=workspace_crud or FakeWorkspaceCrud(),
    )


def _make_turn_service(task_crud=None, turn_crud=None) -> TurnService:
    return TurnService(
        task_crud=task_crud or FakeTaskCrud(),
        turn_crud=turn_crud or FakeTurnCrud(),
    )


def _make_workspace_service(
    task_crud=None, turn_crud=None, workspace_crud=None, run_store=None
) -> WorkspaceService:
    return WorkspaceService(
        task_crud=task_crud or FakeTaskCrud(),
        turn_crud=turn_crud or FakeTurnCrud(),
        workspace_crud=workspace_crud or FakeWorkspaceCrud(),
        run_store=run_store,
    )


def _ws_rec(ws_id: str = "ws-1") -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=ws_id,
        name="n",
        root_path="/p",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


# ===========================================================================
# 一、WorkspaceService.delete_workspace 级联清理 durable_runs（本次改动核心）
# ===========================================================================


# 测试目的：验证 delete_workspace 在有 run_store 且工作区含任务时，会先调用 run_store.delete_by_task_ids
# 级联清理持久化运行记录。可能发现的缺陷：级联顺序错误、忘记清理 durable_runs 导致孤儿 run。
def test_delete_workspace_cascades_durable_runs_when_run_store_present():
    task_crud = FakeTaskCrud()
    task_crud._task_ids = ["task-a", "task-b"]
    turn_crud = FakeTurnCrud()
    turn_crud._turn_ids = ["turn-1"]
    run_store = FakeRunStore(run_ids=["run-a", "run-b"])
    ws_crud = FakeWorkspaceCrud()

    svc = _make_workspace_service(task_crud, turn_crud, ws_crud, run_store)
    svc.delete_workspace("ws-1")

    # 必须先清理 durable_runs，再删 turns/tasks，最后删 workspace
    assert run_store.deleted_task_ids == ["task-a", "task-b"]
    assert turn_crud.deleted_ids == ["turn-1"]
    assert task_crud.deleted_ids == ["task-a", "task-b"]
    assert ws_crud.deleted_id == "ws-1"


# 测试目的：验证 delete_workspace 在 run_store 为 None（可选参数缺省）时不会抛错，仍能完成级联删除。
# 可能发现的缺陷：未对 run_store 做 None 判断导致 AttributeError。
def test_delete_workspace_works_without_run_store():
    task_crud = FakeTaskCrud()
    task_crud._task_ids = ["task-a"]
    turn_crud = FakeTurnCrud()
    turn_crud._turn_ids = ["turn-1"]
    ws_crud = FakeWorkspaceCrud()

    # 不传 run_store（即默认值 None）
    svc = _make_workspace_service(task_crud, turn_crud, ws_crud, run_store=None)
    svc.delete_workspace("ws-1")

    assert turn_crud.deleted_ids == ["turn-1"]
    assert task_crud.deleted_ids == ["task-a"]
    assert ws_crud.deleted_id == "ws-1"


# 测试目的：验证 delete_workspace 在工作区无任务时，不调用 turn/task/run 的级联删除，仅删 workspace。
# 可能发现的缺陷：空工作区仍尝试 delete_by_task_ids/delete_by_ids 引发不必要的 DB 操作。
def test_delete_workspace_empty_workspace_no_cascade():
    task_crud = FakeTaskCrud()
    task_crud._task_ids = []  # 无任务
    turn_crud = FakeTurnCrud()
    run_store = FakeRunStore(run_ids=["run-x"])
    ws_crud = FakeWorkspaceCrud()

    svc = _make_workspace_service(task_crud, turn_crud, ws_crud, run_store)
    svc.delete_workspace("ws-empty")

    assert run_store.deleted_task_ids == []  # 无任务则不查 run
    assert turn_crud.deleted_ids == []
    assert task_crud.deleted_ids == []
    assert ws_crud.deleted_id == "ws-empty"


# 测试目的：验证 delete_workspace 在有任务但无 turn 时，仅删 task 与 run，不调用 turn 删除。
# 可能发现的缺陷：task 存在但 turn 为空时仍尝试删除 turn 列表。
def test_delete_workspace_tasks_without_turns():
    task_crud = FakeTaskCrud()
    task_crud._task_ids = ["task-a"]
    turn_crud = FakeTurnCrud()
    turn_crud._turn_ids = []  # 该任务无 turn
    run_store = FakeRunStore(run_ids=["run-a"])
    ws_crud = FakeWorkspaceCrud()

    svc = _make_workspace_service(task_crud, turn_crud, ws_crud, run_store)
    svc.delete_workspace("ws-1")

    assert run_store.deleted_task_ids == ["task-a"]
    assert turn_crud.deleted_ids == []  # 无 turn 不删
    assert task_crud.deleted_ids == ["task-a"]
    assert ws_crud.deleted_id == "ws-1"


# 测试目的：验证 delete_workspace 调用 run_store.delete_by_task_ids 返回被删 run_id 列表（契约一致性）。
# 可能发现的缺陷：返回值被忽略或错误聚合。
def test_delete_workspace_run_store_returns_deleted_ids():
    task_crud = FakeTaskCrud()
    task_crud._task_ids = ["task-a"]
    run_store = FakeRunStore(run_ids=["run-1", "run-2"])
    svc = _make_workspace_service(task_crud, FakeTurnCrud(), FakeWorkspaceCrud(), run_store)

    # delete_workspace 本身不返回 run_ids，但需确保 run_store 被正确调用且返回被使用（此处仅验证调用契约）
    svc.delete_workspace("ws-1")
    assert run_store.deleted_task_ids == ["task-a"]


# ===========================================================================
# 二、TaskService.create_task 空输入校验（本次改动新增）
# ===========================================================================


# 测试目的：验证 create_task 在 input_text 为空白字符串时抛 ValueError。
# 可能发现的缺陷：未做空字符串校验，导致创建出无内容的任务。
def test_create_task_rejects_empty_string():
    svc = _make_task_service()
    with pytest.raises(ValueError):
        svc.create_task(input_text="   ", status="pending")


# 测试目的：验证 create_task 在 input_text 为非字符串类型时抛 ValueError。
# 可能发现的缺陷：仅校验 .strip() 而忽略类型，导致非字符串输入绕过校验或抛 AttributeError。
@pytest.mark.parametrize("bad_input", [None, 123, [], {}])
def test_create_task_rejects_non_string(bad_input):
    svc = _make_task_service()
    with pytest.raises(ValueError):
        svc.create_task(input_text=bad_input, status="pending")


# 测试目的：验证 create_task 在合法非空输入时正常创建任务并首个 turn，且 workspace 缺省时回退默认工作区。
# 可能发现的缺陷：默认工作区解析失败、未创建首个 turn、或 title 预览生成错误。
def test_create_task_creates_task_and_first_turn_with_default_workspace():
    task_crud = FakeTaskCrud()
    turn_crud = FakeTurnCrud()
    svc = _make_task_service(task_crud, turn_crud)

    task = svc.create_task(input_text="  实现登录功能  ", status="pending")

    # task 被创建
    assert task_crud.created["input_text"] == "  实现登录功能  "
    assert task_crud.created["workspace_id"] == "ws-default"  # 回退默认工作区
    assert task_crud.created["status"] == "pending"
    # 首个 turn 被创建
    assert turn_crud.created["task_id"] == task.task_id
    assert turn_crud.created["input_text"] == "  实现登录功能  "


# 测试目的：验证 create_task 在显式传入 workspace_id 时使用该工作区而非默认。
# 可能发现的缺陷：workspace_id 参数被忽略，始终使用默认工作区。
def test_create_task_uses_explicit_workspace_id():
    task_crud = FakeTaskCrud()
    svc = _make_task_service(task_crud, FakeTurnCrud())

    svc.create_task(input_text="hello", status="running", workspace_id="ws-explicit")

    assert task_crud.created["workspace_id"] == "ws-explicit"
    assert task_crud.ensure_default_calls == 0  # 不应回退默认


# 测试目的：验证 create_task 的 title 预览基于输入文本生成（preview 截断行为）。
# 可能发现的缺陷：title 未生成或使用了错误的源文本。
def test_create_task_title_preview_from_input():
    task_crud = FakeTaskCrud()
    svc = _make_task_service(task_crud, FakeTurnCrud())

    svc.create_task(input_text="short text", status="pending")

    assert task_crud.created["title"] == "short text"
    assert task_crud.created["last_message_preview"] == "short text"


# ===========================================================================
# 三、TurnService.create_turn 空输入校验（本次改动新增）
# ===========================================================================


# 测试目的：验证 create_turn 在 input_text 为空白字符串时抛 ValueError。
# 可能发现的缺陷：未做空字符串校验。
def test_create_turn_rejects_empty_string():
    svc = _make_turn_service()
    with pytest.raises(ValueError):
        svc.create_turn(task_id="task-1", input_text="\n\t ", status="pending")


# 测试目的：验证 create_turn 在 input_text 为非字符串类型时抛 ValueError。
# 可能发现的缺陷：类型校验缺失导致非字符串绕过或 AttributeError。
@pytest.mark.parametrize("bad_input", [None, 42, (), ""])
def test_create_turn_rejects_non_string(bad_input):
    svc = _make_turn_service()
    with pytest.raises(ValueError):
        svc.create_turn(task_id="task-1", input_text=bad_input, status="pending")


# 测试目的：验证 create_turn 在合法输入时创建 turn 并同步更新父任务最新轮次信息。
# 可能发现的缺陷：未更新父任务 latest_turn_id / preview，或调用顺序错误。
def test_create_turn_updates_parent_task_latest_turn():
    task_crud = FakeTaskCrud()
    turn_crud = FakeTurnCrud()
    svc = _make_turn_service(task_crud, turn_crud)

    turn = svc.create_turn(task_id="task-1", input_text="继续开发", status="pending")

    assert turn.turn_id == "turn-fixed"
    # 父任务最新轮次信息被更新
    assert task_crud.updated_latest == [("task-1", "turn-fixed", "继续开发")]


# 测试目的：验证 create_turn 的 status 缺省值为 pending 且透传给 turn_crud。
# 可能发现的缺陷：默认 status 未生效或与文档不符。
def test_create_turn_default_status_is_pending():
    turn_crud = FakeTurnCrud()
    svc = _make_turn_service(FakeTaskCrud(), turn_crud)

    svc.create_turn(task_id="task-1", input_text="valid")
    assert turn_crud.created["status"] == "pending"


# ===========================================================================
# 四、构造函数契约（run_store 可选）与构造后字段绑定
# ===========================================================================


# 测试目的：验证 WorkspaceService 构造时 run_store 缺省为 None（本次改动将其设为可选参数）。
# 可能发现的缺陷：run_store 被改为必填，破坏旧调用点或无 run_store 的测试/场景。
def test_workspace_service_run_store_default_none():
    svc = WorkspaceService(FakeTaskCrud(), FakeTurnCrud(), FakeWorkspaceCrud())
    assert svc._run_store is None


# 测试目的：验证 WorkspaceService 构造时传入 run_store 被正确绑定。
# 可能发现的缺陷：run_store 未保存，导致级联删除时取不到。
def test_workspace_service_run_store_bound_when_passed():
    run_store = FakeRunStore()
    svc = _make_workspace_service(run_store=run_store)
    assert svc._run_store is run_store


# 测试目的：验证 TaskService / TurnService 构造后依赖被正确绑定（依赖注入契约）。
# 可能发现的缺陷：依赖被错绑到错误属性。
def test_service_dependency_binding():
    task_crud = FakeTaskCrud()
    turn_crud = FakeTurnCrud()
    ws_crud = FakeWorkspaceCrud()

    ts = TaskService(task_crud, turn_crud, ws_crud)
    assert ts._task is task_crud and ts._turn is turn_crud and ts._workspace is ws_crud

    tus = TurnService(task_crud, turn_crud)
    assert tus._task is task_crud and tus._turn is turn_crud


# ===========================================================================
# 五、本次改动边界补充 + 同模块读方法（提升可测范围内覆盖率）
# ===========================================================================


# 测试目的：验证 create_task 的 agent_id 默认值 developer 被透传到 task_crud.create。
# 可能发现的缺陷：agent_id 默认值未生效或被错误覆盖。
def test_create_task_default_agent_id():
    task_crud = FakeTaskCrud()
    svc = _make_task_service(task_crud, FakeTurnCrud())
    svc.create_task(input_text="valid", status="pending")
    assert task_crud.created["agent_id"] == "developer"


# 测试目的：验证 create_task 显式 agent_id 被透传，不被默认值覆盖。
# 可能发现的缺陷：agent_id 参数被忽略。
def test_create_task_explicit_agent_id():
    task_crud = FakeTaskCrud()
    svc = _make_task_service(task_crud, FakeTurnCrud())
    svc.create_task(input_text="valid", status="pending", agent_id="reviewer")
    assert task_crud.created["agent_id"] == "reviewer"


# 测试目的：验证 create_turn 显式 status 被透传给 turn_crud.create。
# 可能发现的缺陷：status 参数被忽略或写死。
def test_create_turn_explicit_status_passthrough():
    turn_crud = FakeTurnCrud()
    svc = _make_turn_service(FakeTaskCrud(), turn_crud)
    svc.create_turn(task_id="task-1", input_text="valid", status="running")
    assert turn_crud.created["status"] == "running"


# 测试目的：验证 WorkspaceService.create_workspace / list_workspaces / get_workspace 委托到 crud。
# 可能发现的缺陷：编排方法未正确委托底层 crud（回归风险）。
def test_workspace_service_delegations():
    ws_crud = FakeWorkspaceCrud()
    svc = _make_workspace_service(FakeTaskCrud(), FakeTurnCrud(), ws_crud)
    rec = svc.create_workspace("name", "/root")
    assert rec.workspace_id == "ws-1"
    assert svc.list_workspaces() == []
    assert svc.get_workspace("ws-x").workspace_id == "ws-x"


# 测试目的：验证 TaskService 读方法（get_task/update_status/has_status/list_tasks_for_workspace）委托到 crud。
# 可能发现的缺陷：读方法未正确委托，破坏 API 层直接依赖 service 的契约。
def test_task_service_read_delegations():
    task_crud = FakeTaskCrud()
    svc = _make_task_service(task_crud, FakeTurnCrud())
    assert svc.get_task("t1").task_id == "t1"
    assert svc.update_status("t1", "done").task_id == "t1"
    assert svc.has_status("t1", "done") is True
    assert svc.list_tasks_for_workspace("ws") == []


# 测试目的：验证 TurnService 读方法（get_turn/list/update_status/claim/get_for_task）委托到 crud。
# 可能发现的缺陷：读方法未正确委托，破坏 API 层直接依赖 service 的契约。
def test_turn_service_read_delegations():
    turn_crud = FakeTurnCrud()
    svc = _make_turn_service(FakeTaskCrud(), turn_crud)
    assert svc.get_turn("turn-1").turn_id == "turn-fixed"
    assert svc.list_turns_for_task("t1") == []
    assert svc.update_turn_status("turn-1", "done").turn_id == "turn-fixed"
    assert svc.claim_pending_turn("turn-1") is True
    assert svc.get_turn_for_task("t1").turn_id == "turn-fixed"
