"""针对「状态模型收敛 + Checkpoint 利用」重构的隔离单元测试。

重要约束：
- 仓库处于未完成的 domain→service 大重构中，存在预存坏导入
  ``app/config/logging/save/sqlite_handler.py:12``（误写为
  ``from app.storage.crud.log import LogStore``，正确应为 ``log_crud``），该坏导入位于
  ``app.config.logging.__init__`` 导入链上，会导致 ``import app.api.*`` /
  ``import app.core.runtime.runner`` 在导入期失败。
- 因此本测试**不导入**任何依赖 ``app.config.logging`` 的模块（如 ``app.api.app``、
  ``app.api.dependencies``、``app.core.runtime.runner``），仅隔离测试不触发该坏导入链、且属于
  本次改动行为契约的纯逻辑：``app.service.task.*`` 三个 service 类、``TurnMessageCrud``、
  ``TurnCrud`` 的 ``end_reason`` / ``get_latest_turn``。
- 这些 service 类通过构造注入 crud/store 实例，本测试用轻量 fake 对象替代，无需真实 SQLite
  （``TurnMessageCrud`` / ``TurnCrud`` 的存储契约用临时 SQLite 验证）。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from app.models import RuntimeMessage, TaskRecord, TurnRecord, WorkspaceRecord
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
        self._store: dict[str, TaskRecord] = {}

    def create(self, **kwargs: Any) -> TaskRecord:
        self.created = kwargs
        record = TaskRecord(
            task_id=kwargs["task_id"],
            workspace_id=kwargs["workspace_id"],
            agent_id=kwargs["agent_id"],
            input_text=kwargs["input_text"],
            title=kwargs["title"],
            last_message_preview=kwargs["last_message_preview"],
            latest_turn_id=kwargs["latest_turn_id"],
            status=kwargs["status"],
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._store[record.task_id] = record
        return record

    def ensure_default(self, now: datetime) -> str:
        self.ensure_default_calls += 1
        return self._default_workspace_id

    def list_ids_by_workspace(self, workspace_id: str) -> list[str]:
        return [tid for tid, rec in self._store.items() if rec.workspace_id == workspace_id]

    def delete_by_ids(self, task_ids: list[str]) -> None:
        self.deleted_ids = list(task_ids)

    def update_latest_turn(self, task_id: str, latest_turn_id: str, preview: str) -> None:
        self.updated_latest.append((task_id, latest_turn_id, preview))

    def get(self, task_id: str) -> TaskRecord:
        if task_id not in self._store:
            raise KeyError(task_id)
        return self._store[task_id]

    def update_status(self, task_id: str, status: str) -> TaskRecord:
        self._store[task_id] = replace(self._store[task_id], status=status)
        return self._store[task_id]

    def has_status(self, task_id: str, status: str) -> bool:
        return self._store.get(task_id).status == status

    def list_by_workspace(self, workspace_id: str) -> list[TaskRecord]:
        return []


class FakeTurnCrud:
    """记录调用并返回可预测结果的 TurnCrud 替身，内部按 turn_id 维护轮次。"""

    def __init__(self) -> None:
        self.created: dict[str, Any] = {}
        self.deleted_ids: list[str] = []
        self.updated_status: list[tuple[str, str, str | None]] = []
        self._turns: dict[str, TurnRecord] = {}
        self._seq = 0

    def _new_id(self) -> str:
        self._seq += 1
        return f"turn-{self._seq}"

    def create(self, task_id: str, input_text: str, status: str = "pending") -> TurnRecord:
        turn_id = self._new_id()
        turn = TurnRecord(
            turn_id=turn_id,
            task_id=task_id,
            input_text=input_text,
            status=status,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._turns[turn_id] = turn
        self.created = {
            "task_id": task_id,
            "input_text": input_text,
            "status": status,
            "turn_id": turn_id,
        }
        return turn

    def get(self, turn_id: str) -> TurnRecord:
        if turn_id not in self._turns:
            raise KeyError(turn_id)
        return self._turns[turn_id]

    def list_by_task(self, task_id: str) -> list[TurnRecord]:
        return [t for t in self._turns.values() if t.task_id == task_id]

    def list_ids_by_task_ids(self, task_ids: list[str]) -> list[str]:
        ids = set(task_ids)
        return [t.turn_id for t in self._turns.values() if t.task_id in ids]

    def delete_by_ids(self, turn_ids: list[str]) -> None:
        self.deleted_ids = list(turn_ids)

    def update_status(self, turn_id: str, status: str, end_reason: str | None = None) -> TurnRecord:
        self._turns[turn_id] = replace(
            self._turns[turn_id], status=status, end_reason=end_reason
        )
        self.updated_status.append((turn_id, status, end_reason))
        return self._turns[turn_id]

    def claim_pending(self, turn_id: str) -> bool:
        return True

    def get_first_for_task(self, task_id: str) -> TurnRecord:
        turns = self.list_by_task(task_id)
        if not turns:
            raise KeyError(task_id)
        return turns[0]

    def get_latest_turn(self, task_id: str) -> TurnRecord:
        turns = self.list_by_task(task_id)
        if not turns:
            raise KeyError(task_id)
        return turns[-1]


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
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def list_all(self) -> list[WorkspaceRecord]:
        return []

    def get(self, workspace_id: str) -> WorkspaceRecord:
        return WorkspaceRecord(
            workspace_id=workspace_id,
            name="n",
            root_path="/p",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def delete(self, workspace_id: str) -> None:
        self.deleted_id = workspace_id


class FakeMessageCrud:
    """记录调用并返回可预测结果的 TurnMessageCrud 替身。"""

    def __init__(self) -> None:
        self.saved: dict[str, list[RuntimeMessage]] = {}
        self.loaded: dict[str, list[RuntimeMessage]] = {}

    def save_messages(self, turn_id: str, messages: list[RuntimeMessage]) -> None:
        self.saved[turn_id] = list(messages)

    def load_messages(self, turn_id: str) -> list[RuntimeMessage]:
        return list(self.loaded.get(turn_id, []))


def _make_task_service(task_crud=None, turn_crud=None, workspace_crud=None) -> TaskService:
    return TaskService(
        task_crud=task_crud or FakeTaskCrud(),
        turn_crud=turn_crud or FakeTurnCrud(),
        workspace_crud=workspace_crud or FakeWorkspaceCrud(),
    )


def _make_turn_service(task_crud=None, turn_crud=None, message_crud=None) -> TurnService:
    return TurnService(
        task_crud=task_crud or FakeTaskCrud(),
        turn_crud=turn_crud or FakeTurnCrud(),
        message_crud=message_crud,
    )


def _make_workspace_service(
    task_crud=None, turn_crud=None, workspace_crud=None
) -> WorkspaceService:
    return WorkspaceService(
        task_crud=task_crud or FakeTaskCrud(),
        turn_crud=turn_crud or FakeTurnCrud(),
        workspace_crud=workspace_crud or FakeWorkspaceCrud(),
    )


def _ws_rec(ws_id: str = "ws-1") -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=ws_id,
        name="n",
        root_path="/p",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# ===========================================================================
# 一、TaskService 生命周期（open/archived）与执行态派生（本次改动核心）
# ===========================================================================


# 测试目的：验证 set_lifecycle_status 把任务状态写为 open/archived。
# 可能发现的缺陷：set_lifecycle_status 未接线或写错列。
def test_set_lifecycle_status_open_and_archived():
    svc = _make_task_service()
    task = svc.create_task(input_text="hello", status="open")
    rec = svc.set_lifecycle_status(task.task_id, "archived")
    assert rec.status == "archived"
    rec2 = svc.set_lifecycle_status(rec.task_id, "open")
    assert rec2.status == "open"


# 测试目的：验证 set_lifecycle_status 拒绝非法生命周期值。
# 可能发现的缺陷：未校验 lifecycle 取值，导致写入执行态字符串污染语义。
@pytest.mark.parametrize("bad", ["running", "completed", "cancelled", "pending"])
def test_set_lifecycle_status_rejects_invalid(bad):
    svc = _make_task_service()
    task = svc.create_task(input_text="hello", status="open")
    with pytest.raises(ValueError):
        svc.set_lifecycle_status(task.task_id, bad)


# 测试目的：验证 task_display_status 从最新 turn 派生执行态。
# 可能发现的缺陷：派生规则错误（running→active 映射缺失、无 turn→empty 等）。
@pytest.mark.parametrize(
    "turn_status,expected",
    [
        ("running", "active"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_task_display_status_derives_from_latest_turn(turn_status, expected):
    task_crud = FakeTaskCrud()
    turn_crud = FakeTurnCrud()
    svc = _make_task_service(task_crud, turn_crud)
    task = svc.create_task(input_text="hello", status="open")
    turn_crud.create(task.task_id, "hello", status=turn_status)
    assert svc.task_display_status(task.task_id) == expected


# 测试目的：验证 task_display_status 在无 turn 时返回 empty。
# 可能发现的缺陷：无 turn 时抛 KeyError 或返回错误值。
def test_task_display_status_empty_without_turns():
    task_crud = FakeTaskCrud()
    turn_crud = FakeTurnCrud()
    svc = _make_task_service(task_crud, turn_crud)
    # 手动注册一个任务但不创建任何 turn（验证无轮次时派生为空）
    task_crud._store["task-x"] = TaskRecord(
        task_id="task-x", workspace_id="ws", agent_id="developer", input_text="x",
        title="t", last_message_preview="p", latest_turn_id=None, status="open",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    assert svc.task_display_status("task-x") == "empty"


# 测试目的：验证 get_task 返回字典含派生的 execution_status。
# 可能发现的缺陷：to_dict 未包含 execution_status，或派生未注入。
def test_get_task_includes_execution_status():
    task_crud = FakeTaskCrud()
    turn_crud = FakeTurnCrud()
    svc = _make_task_service(task_crud, turn_crud)
    task = svc.create_task(input_text="hello", status="open")
    turn_crud.create(task.task_id, "hello", status="running")
    result = svc.get_task(task.task_id).to_dict()
    assert result["status"] == "open"
    assert result["execution_status"] == "active"


# ===========================================================================
# 二、TaskService.create_task 空输入校验（保留）
# ===========================================================================


def test_create_task_rejects_empty_string():
    svc = _make_task_service()
    with pytest.raises(ValueError):
        svc.create_task(input_text="   ", status="open")


@pytest.mark.parametrize("bad_input", [None, 123, [], {}])
def test_create_task_rejects_non_string(bad_input):
    svc = _make_task_service()
    with pytest.raises(ValueError):
        svc.create_task(input_text=bad_input, status="open")


def test_create_task_creates_task_and_first_turn_with_default_workspace():
    task_crud = FakeTaskCrud()
    turn_crud = FakeTurnCrud()
    svc = _make_task_service(task_crud, turn_crud)

    task = svc.create_task(input_text="  实现登录功能  ", status="open")

    # task 被创建，生命周期状态为 open
    assert task_crud.created["status"] == "open"
    assert task_crud.created["workspace_id"] == "ws-default"
    # 首个 turn 被创建，执行态固定 pending
    assert turn_crud.created["task_id"] == task.task_id
    assert turn_crud.created["status"] == "pending"


# ===========================================================================
# 三、TurnService.get_latest_turn / has_turn_status / 消息轨迹透传（本次改动）
# ===========================================================================


# 测试目的：验证 get_latest_turn 返回创建时间最新的轮（而非第一轮）。
# 可能发现的缺陷：仍返回第一轮（历史 bug 复现）。
def test_turn_service_get_latest_turn_returns_newest():
    turn_crud = FakeTurnCrud()
    svc = _make_turn_service(FakeTaskCrud(), turn_crud)
    t1 = turn_crud.create("task-1", "first", status="completed")
    t2 = turn_crud.create("task-1", "second", status="running")
    assert svc.get_latest_turn("task-1").turn_id == t2.turn_id
    assert svc.get_latest_turn("task-1").turn_id != t1.turn_id


# 测试目的：验证 has_turn_status 比对 turn 当前状态。
# 可能发现的缺陷：误比对 task 状态或始终返回 True。
def test_turn_service_has_turn_status():
    turn_crud = FakeTurnCrud()
    svc = _make_turn_service(FakeTaskCrud(), turn_crud)
    turn = turn_crud.create("task-1", "x", status="running")
    assert svc.has_turn_status(turn.turn_id, "running") is True
    assert svc.has_turn_status(turn.turn_id, "completed") is False


# 测试目的：验证 update_turn_status 携带 end_reason（终态原因）。
# 可能发现的缺陷：end_reason 未透传或被丢弃。
def test_turn_service_update_turn_status_with_end_reason():
    turn_crud = FakeTurnCrud()
    svc = _make_turn_service(FakeTaskCrud(), turn_crud)
    turn = turn_crud.create("task-1", "x", status="running")
    updated = svc.update_turn_status(turn.turn_id, "cancelled", end_reason="user_cancelled")
    assert updated.status == "cancelled"
    assert updated.end_reason == "user_cancelled"
    assert turn_crud.updated_status[-1] == (turn.turn_id, "cancelled", "user_cancelled")


# 测试目的：验证 save/load_turn_messages 透传到 message_crud。
# 可能发现的缺陷：message_crud 未接线导致轨迹丢失。
def test_turn_service_message_store_passthrough():
    message_crud = FakeMessageCrud()
    svc = _make_turn_service(message_crud=message_crud)
    msgs = [
        RuntimeMessage(role="user", content_text="hi"),
        RuntimeMessage(role="assistant", content_text="ok", metadata={"tool_call_id": "c1"}),
    ]
    svc.save_turn_messages("turn-1", msgs)
    assert message_crud.saved["turn-1"] == msgs
    message_crud.loaded["turn-1"] = msgs
    assert svc.load_turn_messages("turn-1") == msgs


# 测试目的：验证 message_crud 为 None 时 save/load 安全降级（不抛）。
# 可能发现的缺陷：None 时 AttributeError。
def test_turn_service_message_store_none_safe():
    svc = _make_turn_service(message_crud=None)
    svc.save_turn_messages("turn-1", [RuntimeMessage(role="user", content_text="hi")])
    assert svc.load_turn_messages("turn-1") == []


# ===========================================================================
# 四、WorkspaceService.delete_workspace 级联（移除 durable run，仅 task+turn）
# ===========================================================================


def test_delete_workspace_cascades_turn_and_task():
    task_crud = FakeTaskCrud()
    task_crud._store["task-a"] = TaskRecord(
        task_id="task-a", workspace_id="ws-1", agent_id="developer", input_text="x",
        title="t", last_message_preview="p", latest_turn_id=None, status="open",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    task_crud._store["task-b"] = TaskRecord(
        task_id="task-b", workspace_id="ws-1", agent_id="developer", input_text="x",
        title="t", last_message_preview="p", latest_turn_id=None, status="open",
        created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    turn_crud = FakeTurnCrud()
    turn_crud.create("task-a", "x")
    turn_crud.create("task-b", "x")
    ws_crud = FakeWorkspaceCrud()

    svc = _make_workspace_service(task_crud, turn_crud, ws_crud)
    svc.delete_workspace("ws-1")

    assert turn_crud.deleted_ids == ["turn-1", "turn-2"]
    assert task_crud.deleted_ids == ["task-a", "task-b"]
    assert ws_crud.deleted_id == "ws-1"


def test_delete_workspace_empty_no_cascade():
    task_crud = FakeTaskCrud()
    turn_crud = FakeTurnCrud()
    ws_crud = FakeWorkspaceCrud()

    svc = _make_workspace_service(task_crud, turn_crud, ws_crud)
    svc.delete_workspace("ws-empty")

    assert turn_crud.deleted_ids == []
    assert task_crud.deleted_ids == []
    assert ws_crud.deleted_id == "ws-empty"


# ===========================================================================
# 五、构造函数契约与读方法委托（保留 + 适配新签名）
# ===========================================================================


def test_service_dependency_binding():
    task_crud = FakeTaskCrud()
    turn_crud = FakeTurnCrud()
    ws_crud = FakeWorkspaceCrud()

    ts = TaskService(task_crud, turn_crud, ws_crud)
    assert ts._task is task_crud and ts._turn is turn_crud and ts._workspace is ws_crud

    tus = TurnService(task_crud, turn_crud)
    assert tus._task is task_crud and tus._turn is turn_crud


def test_task_service_read_delegations():
    task_crud = FakeTaskCrud()
    svc = _make_task_service(task_crud, FakeTurnCrud())
    svc.create_task(input_text="valid", status="open")
    tid = next(iter(task_crud._store))
    assert svc.get_task(tid).task_id == tid
    assert svc.update_status(tid, "archived").task_id == tid
    assert svc.has_status(tid, "archived") is True
    assert svc.list_tasks_for_workspace("ws") == []


def test_turn_service_read_delegations():
    turn_crud = FakeTurnCrud()
    svc = _make_turn_service(FakeTaskCrud(), turn_crud)
    turn = turn_crud.create("task-1", "valid", status="pending")
    assert svc.get_turn(turn.turn_id).turn_id == turn.turn_id
    assert svc.list_turns_for_task("task-1") == [turn]
    assert svc.update_turn_status(turn.turn_id, "done").turn_id == turn.turn_id
    assert svc.claim_pending_turn(turn.turn_id) is True


# ===========================================================================
# 六、TurnMessageCrud 真实 SQLite 契约（新建表 + 读写 round-trip）
# ===========================================================================


def _init_temp_storage():
    """用临时 SQLite 文件初始化存储，返回清理函数。"""

    from dataclasses import replace as dcreplace
    from pathlib import Path

    from app.config.settings import default_settings
    from app.storage.store_engines import close_storage, init_storage

    tmp = tempfile.mkdtemp()
    settings = default_settings()
    settings = dcreplace(
        settings,
        database_file=Path(os.path.join(tmp, "main.sqlite")),
        log_database_file=Path(os.path.join(tmp, "log.sqlite")),
        checkpoint_file=Path(os.path.join(tmp, "checkpoint.sqlite")),
    )
    init_storage(settings)
    return tmp, close_storage


def _seed_workspace() -> str:
    """创建默认工作区并返回其标识，供需要外键父行的真实 SQLite 测试使用。"""

    from datetime import datetime

    from app.storage.crud.workspace_crud import WorkspaceCrud

    return WorkspaceCrud().ensure_default(datetime.now(UTC))


def test_turn_message_crud_roundtrip():
    _, close = _init_temp_storage()
    try:
        from app.storage.crud.task_crud import TaskCrud
        from app.storage.crud.turn_crud import TurnCrud
        from app.storage.crud.turn_message_crud import TurnMessageCrud

        workspace_id = _seed_workspace()
        task = TaskCrud().create(
            task_id="task-x",
            workspace_id=workspace_id,
            agent_id="developer",
            input_text="x",
            title="t",
            last_message_preview="p",
            latest_turn_id="",
            status="open",
        )
        turn_id = TurnCrud().create(task.task_id, "first").turn_id

        crud = TurnMessageCrud()
        messages = [
            RuntimeMessage(role="system", content_text="sys"),
            RuntimeMessage(role="user", content_text="hello"),
            RuntimeMessage(
                role="assistant",
                content_text="tool call",
                metadata={"tool_call_id": "c1", "tool_calls": "[]"},
            ),
        ]
        crud.save_messages(turn_id, messages)
        loaded = crud.load_messages(turn_id)
        assert [m.role for m in loaded] == ["system", "user", "assistant"]
        assert loaded[1].content_text == "hello"
        assert loaded[2].metadata.get("tool_call_id") == "c1"

        # 覆盖式保存：再次保存更少消息应替换而非追加
        crud.save_messages(turn_id, [RuntimeMessage(role="user", content_text="again")])
        reloaded = crud.load_messages(turn_id)
        assert len(reloaded) == 1
        assert reloaded[0].content_text == "again"
    finally:
        close()


def test_turn_crud_end_reason_and_latest():
    _, close = _init_temp_storage()
    try:
        from app.storage.crud.task_crud import TaskCrud
        from app.storage.crud.turn_crud import TurnCrud

        workspace_id = _seed_workspace()
        TaskCrud().create(
            task_id="task-z",
            workspace_id=workspace_id,
            agent_id="developer",
            input_text="x",
            title="t",
            last_message_preview="p",
            latest_turn_id="",
            status="open",
        )
        crud = TurnCrud()
        t1 = crud.create("task-z", "first", status="completed")
        crud.update_status(t1.turn_id, "completed", end_reason="done")
        t2 = crud.create("task-z", "second", status="running")

        latest = crud.get_latest_turn("task-z")
        assert latest.turn_id == t2.turn_id

        reread = crud.get(t1.turn_id)
        assert reread.end_reason == "done"
    finally:
        close()
