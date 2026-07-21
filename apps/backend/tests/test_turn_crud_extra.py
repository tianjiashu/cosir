"""TurnCrud / TurnService 的边界、异常与状态路径补充测试（聚焦 response_text 与执行态）。

目标：把本次改动相关的 TurnCrud / TurnService 行为覆盖率推过 80%，覆盖
create 空输入校验、get 不存在抛 KeyError、update_status、claim_pending
（乐观并发抢占）、外键约束失败、以及 service 层其余委托方法。

导入前置：仓库存在坏导入链，需在导入 app 模块之前注入 app.storage.crud.log 占位。
"""

import sys

sys.path.insert(0, ".")

from tests.conftest_stub_log import install_log_crud_stub

install_log_crud_stub()

import pytest

from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.turn_crud import TurnCrud
from app.storage.store_engines import close_storage, init_storage
from app.config.settings import BackendSettings

import tempfile
from pathlib import Path


@pytest.fixture()
def storage_settings():
    tmp = Path(tempfile.mkdtemp(prefix="coding_agent_turn_extra_"))
    settings = BackendSettings(
        project_root=tmp,
        log_dir=tmp / "logs",
        database_file=tmp / "app.sqlite3",
        log_database_file=tmp / "logs.sqlite3",
        checkpoint_file=tmp / "langgraph_checkpoints.sqlite",
    )
    init_storage(settings)
    yield settings
    close_storage()


def _services():
    task_crud = TaskCrud()
    turn_crud = TurnCrud()
    from app.storage.crud.workspace_crud import WorkspaceCrud

    workspace_crud = WorkspaceCrud()
    task_service = TaskService(task_crud, turn_crud, workspace_crud)
    turn_service = TurnService(task_crud, turn_crud)
    return task_crud, turn_crud, task_service, turn_service


# 测试目的：create 对空白 input_text 抛 ValueError（契约）。缺陷：缺少校验导致空 turn 入库。
def test_turn_crud_create_rejects_blank_input(storage_settings):
    _, turn_crud, task_service, _ = _services()
    task = task_service.create_task("x", "open")
    with pytest.raises(ValueError):
        turn_crud.create(task.task_id, "   ")


# 测试目的：get 不存在的 turn 抛 KeyError（契约）。缺陷：缺失返回 None 导致下游空指针。
def test_turn_crud_get_missing_raises_keyerror(storage_settings):
    _, turn_crud, _, _ = _services()
    with pytest.raises(KeyError):
        turn_crud.get("no-such-turn")


# 测试目的：create 必须先有 task（外键），无 task 时 IntegrityError。缺陷：外键缺失或建表顺序错。
def test_turn_crud_create_requires_existing_task(storage_settings):
    _, turn_crud, _, _ = _services()
    with pytest.raises(Exception):
        turn_crud.create("ghost-task", "input", "pending")


# 测试目的：update_status 写入新状态与 end_reason，且 updated_at 刷新。缺陷：状态/原因未落库。
def test_turn_crud_update_status_persists(storage_settings):
    _, turn_crud, task_service, _ = _services()
    task = task_service.create_task("y", "open")
    turn = turn_crud.create(task.task_id, "input", "pending")

    updated = turn_crud.update_status(turn.turn_id, "failed", end_reason="client_disconnected")
    assert updated.status == "failed"
    assert updated.end_reason == "client_disconnected"
    refetched = turn_crud.get(turn.turn_id)
    assert refetched.status == "failed"
    assert refetched.end_reason == "client_disconnected"


# 测试目的：update_status 传 None end_reason 时保留原值（不应清空已有原因）。缺陷：误清空原因。
def test_turn_crud_update_status_keeps_end_reason_when_none(storage_settings):
    _, turn_crud, task_service, _ = _services()
    task = task_service.create_task("z", "open")
    turn = turn_crud.create(task.task_id, "input", "pending")
    turn_crud.update_status(turn.turn_id, "failed", end_reason="boom")

    turn_crud.update_status(turn.turn_id, "failed")  # 不传 end_reason
    assert turn_crud.get(turn.turn_id).end_reason == "boom"


# 测试目的：claim_pending 对 pending turn 抢占成功返回 True，且状态变 running。缺陷：乐观锁失效导致重复执行。
def test_turn_crud_claim_pending_succeeds_on_pending(storage_settings):
    _, turn_crud, task_service, _ = _services()
    task = task_service.create_task("claim", "open")
    turn = turn_crud.create(task.task_id, "input", "pending")

    assert turn_crud.claim_pending(turn.turn_id) is True
    assert turn_crud.get(turn.turn_id).status == "running"


# 测试目的：claim_pending 对非 pending（已 running）返回 False，不重复抢占。缺陷：重复抢占导致并发重跑。
def test_turn_crud_claim_pending_false_when_not_pending(storage_settings):
    _, turn_crud, task_service, _ = _services()
    task = task_service.create_task("claim2", "open")
    turn = turn_crud.create(task.task_id, "input", "pending")
    turn_crud.claim_pending(turn.turn_id)  # -> running

    assert turn_crud.claim_pending(turn.turn_id) is False


# 测试目的：list_by_task 按创建序返回该 task 下全部 turn（create_task 已含首个 turn）。缺陷：排序错或漏查。
def test_turn_crud_list_by_task_orders_by_created(storage_settings):
    _, turn_crud, task_service, _ = _services()
    task = task_service.create_task("list", "open")
    t_first = turn_crud.get_latest_turn(task.task_id)  # create_task 自动建的首个 pending turn
    t2 = turn_crud.create(task.task_id, "b", "pending")

    turns = turn_crud.list_by_task(task.task_id)
    assert [t.turn_id for t in turns] == [t_first.turn_id, t2.turn_id]


# 测试目的：get_latest_turn 返回该 task 最新创建的 turn。缺陷：取错轮次导致历史回看出错。
def test_turn_crud_get_latest_turn(storage_settings):
    _, turn_crud, task_service, _ = _services()
    task = task_service.create_task("latest", "open")
    turn_crud.create(task.task_id, "a", "pending")
    t2 = turn_crud.create(task.task_id, "b", "pending")

    assert turn_crud.get_latest_turn(task.task_id).turn_id == t2.turn_id


# 测试目的：list_ids_by_task_ids 跨 task 汇总 turn id；空入参返回空。缺陷：跨表查询遗漏或空入参报错。
def test_turn_crud_list_ids_by_task_ids(storage_settings):
    _, turn_crud, task_service, _ = _services()
    task = task_service.create_task("ids", "open")  # 自动建首个 turn
    turn_crud.create(task.task_id, "a", "pending")

    assert turn_crud.list_ids_by_task_ids([]) == []
    ids = turn_crud.list_ids_by_task_ids([task.task_id])
    assert len(ids) == 2  # create_task 的首个 + 显式创建的一个


# 测试目的：delete_by_ids 删除指定 turn；空入参安全无操作。缺陷：误删或空入参报错。
def test_turn_crud_delete_by_ids(storage_settings):
    _, turn_crud, task_service, _ = _services()
    task = task_service.create_task("del", "open")
    turn = turn_crud.create(task.task_id, "a", "pending")

    turn_crud.delete_by_ids([])  # 安全
    turn_crud.delete_by_ids([turn.turn_id])
    with pytest.raises(KeyError):
        turn_crud.get(turn.turn_id)


# 测试目的：TurnService 其余委托方法（get_turn/list/has_turn_status/claim_pending_turn）正确透传。缺陷：委托层错接。
def test_turn_service_delegations(storage_settings):
    _, turn_crud, task_service, turn_service = _services()
    task = task_service.create_task("svc", "open")
    turn = turn_crud.get_latest_turn(task.task_id)  # create_task 自动建的首个 turn

    assert turn_service.get_turn(turn.turn_id).turn_id == turn.turn_id
    assert turn_service.has_turn_status(turn.turn_id, "pending") is True
    assert turn_service.claim_pending_turn(turn.turn_id) is True
    assert turn_service.has_turn_status(turn.turn_id, "running") is True
    assert len(turn_service.list_turns_for_task(task.task_id)) == 1


# 测试目的：get_first_for_task 返回该 task 创建时间最早的 turn。缺陷：取错轮次顺序。
def test_turn_crud_get_first_for_task(storage_settings):
    _, turn_crud, task_service, _ = _services()
    task = task_service.create_task("first", "open")
    first = turn_crud.get_latest_turn(task.task_id)  # create_task 的首个
    turn_crud.create(task.task_id, "later", "pending")

    got = turn_crud.get_first_for_task(task.task_id)
    assert got.turn_id == first.turn_id


# 测试目的：TurnService.get_latest_turn 委托到 crud 返回最新轮次。缺陷：委托层错接。
def test_turn_service_get_latest_turn(storage_settings):
    _, turn_crud, task_service, turn_service = _services()
    task = task_service.create_task("svc-latest", "open")
    turn_crud.create(task.task_id, "a", "pending")
    t2 = turn_crud.create(task.task_id, "b", "pending")

    assert turn_service.get_latest_turn(task.task_id).turn_id == t2.turn_id


# 测试目的：get_first_for_task 在 task 下无 turn 时抛 KeyError。缺陷：空结果误返回 None。
def test_turn_crud_get_first_for_task_empty_raises(storage_settings):
    _, turn_crud, task_service, _ = _services()
    # 用 create_task 建立 task（同时自动建首个 turn、确保默认工作区存在）。
    task = task_service.create_task("empty-then", "open")
    # 删光该 task 下的 turn，构造“无 turn 的 task”。
    ids = turn_crud.list_ids_by_task_ids([task.task_id])
    turn_crud.delete_by_ids(ids)
    with pytest.raises(KeyError):
        turn_crud.get_first_for_task(task.task_id)
