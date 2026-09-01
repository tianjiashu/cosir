"""Assistant Transport 命令幂等服务测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models.conversation_command_record import ConversationCommandRecord
from app.service.task.conversation_command_service import (
    ConversationCommandService,
    service_depends,
)
from app.storage.crud.conversation_command_crud import ConversationCommandCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.init_schema import initialize_app_schema
from app.storage.model.task_model import TaskModel
from app.storage.model.workspace_model import WorkspaceModel


class FakeCommandCrud:
    """用于隔离测试服务编排的内存命令仓储。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[int, str], SimpleNamespace] = {}
        self.next_id = 1

    def get(self, task_id: int, command_id: str):
        return self.rows.get((task_id, command_id))

    def reserve(self, task_id: int, command_id: str, command_type: str, payload_hash: str):
        row = SimpleNamespace(
            id=self.next_id,
            task_id=task_id,
            command_id=command_id,
            command_type=command_type,
            payload_hash=payload_hash,
            turn_id=None,
            status="processing",
        )
        self.next_id += 1
        self.rows[(task_id, command_id)] = row
        return row

    def bind_turn(self, record_id: int, turn_id: int) -> None:
        for row in self.rows.values():
            if row.id == record_id:
                row.turn_id = turn_id

    def mark_failed(self, record_id: int, error_code: str) -> None:
        for row in self.rows.values():
            if row.id == record_id:
                row.status = "failed"

    def mark_status(self, record_id: int, status: str) -> None:
        for row in self.rows.values():
            if row.id == record_id:
                row.status = status


def make_service(monkeypatch):
    """构造使用内存仓储的命令服务。"""
    crud = FakeCommandCrud()
    monkeypatch.setattr(
        "app.service.task.conversation_command_service.service_depends.get_conversation_command_crud",
        lambda: crud,
    )
    return ConversationCommandService(), crud


def test_reserve_or_get_is_idempotent_within_task(monkeypatch) -> None:
    """同一 task 的重复 command ID 必须复用同一条持久化映射。"""
    service, crud = make_service(monkeypatch)

    first, created = service.reserve_or_get(1, "cmd-1", "add-message", "hash-1")
    second, duplicate = service.reserve_or_get(1, "cmd-1", "add-message", "hash-1")

    assert created is True
    assert duplicate is False
    assert first.id == second.id
    assert len(crud.rows) == 1


def test_command_id_can_repeat_across_tasks(monkeypatch) -> None:
    """不同 task 可以使用相同 command ID，幂等边界属于 task 聚合。"""
    service, crud = make_service(monkeypatch)

    first, _ = service.reserve_or_get(1, "cmd-1", "add-message", "hash-1")
    second, _ = service.reserve_or_get(2, "cmd-1", "add-message", "hash-1")

    assert first.id != second.id
    assert len(crud.rows) == 2


def test_same_command_id_with_different_payload_is_rejected(monkeypatch) -> None:
    """同一 command id 携带不同载荷时必须拒绝，避免错误复用。"""
    import pytest

    from app.service.task.conversation_run_service import CommandPayloadConflictError

    service, crud = make_service(monkeypatch)

    first, created = service.reserve_or_get(1, "cmd-1", "add-message", "hash-1")
    with pytest.raises(CommandPayloadConflictError):
        service.reserve_or_get(1, "cmd-1", "add-message", "hash-2")

    assert created is True
    assert first.payload_hash == "hash-1"
    assert len(crud.rows) == 1


def _real_crud(tmp_path: Path) -> ConversationCommandCrud:
    """构造绑定临时真实 SQLite 引擎的命令 CRUD，供并发/唯一性集成测试使用。

    绕过需要全局 storage 已初始化的默认构造器，直接以临时引擎装配实例，
    使测试不依赖全局 ``init_storage`` 状态。同时写入最小合法 ``workspace`` /
    ``task`` 行以满足 ``conversation_commands.task_id`` 的外键约束，使测试
    覆盖真实的唯一索引与并发回退行为。
    """
    engine = create_sqlite_engine(tmp_path / "app.sqlite3")
    initialize_app_schema(engine)
    with Session(engine) as session:
        workspace = WorkspaceModel(name="test", root_path=str(tmp_path))
        session.add(workspace)
        session.flush()
        session.add(
            TaskModel(
                id=1,
                workspace_id=workspace.id,
                title="test-task",
                status="idle",
            )
        )
        session.commit()
    crud = ConversationCommandCrud.__new__(ConversationCommandCrud)
    crud._session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return crud


def test_concurrent_reserve_is_rejected_by_unique_index(tmp_path: Path) -> None:
    """真实数据库层：相同 (task_id, command_id) 的第二次 reserve 被唯一索引拒绝。

    验证 ``conversation_commands`` 表的 ``uq_conversation_commands_task_command``
    唯一索引生效——这是 command 幂等的最终屏障，确保并发请求无法插入第二条记录。
    """
    crud = _real_crud(tmp_path)

    crud.reserve(1, "cmd-1", "add-message", "hash-1")
    with pytest.raises(IntegrityError):
        crud.reserve(1, "cmd-1", "add-message", "hash-2")


def test_reserve_or_get_falls_back_on_concurrent_conflict(monkeypatch, tmp_path: Path) -> None:
    """并发竞态下（get 未命中但 reserve 撞唯一索引）reserve_or_get 回退到既有记录。

    模拟真实窗口：另一条请求在 ``get`` 与 ``reserve`` 之间抢先插入了同一 command_id。
    此时 ``reserve`` 抛出 ``IntegrityError``，服务必须回退查询并返回已存在的记录，
    而非抛错或写入第二条——这是 plan §7「相同 commandId 不创建第二个 run」的并发保障。
    """
    crud = _real_crud(tmp_path)
    monkeypatch.setattr(service_depends, "get_conversation_command_crud", lambda: crud)
    service = ConversationCommandService()

    first, created = service.reserve_or_get(1, "cmd-1", "add-message", "hash-1")
    assert created is True

    # 模拟竞态窗口：第一次 get 返回 None（另一请求在 get 与 reserve 之间已插入），
    # 之后恢复真实查询——reserve 撞唯一索引抛 IntegrityError 时，回退查询应能命中
    # 已存在的记录。
    real_get = crud.get
    calls = {"n": 0}

    def _miss_once(task_id: int, command_id: str) -> ConversationCommandRecord | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_get(task_id, command_id)

    monkeypatch.setattr(crud, "get", _miss_once)
    import pytest

    from app.service.task.conversation_command_service import CommandPayloadConflictError

    with pytest.raises(CommandPayloadConflictError):
        service.reserve_or_get(1, "cmd-1", "add-message", "hash-2")
    # 验证确实只存在一条记录（未被并发创建第二条）。
    persisted = crud.get(1, "cmd-1")
    assert persisted is not None
    assert persisted.id == first.id
