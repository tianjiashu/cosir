"""create_task_with_initial_turn 业务编排单元测试。

验证 `TaskService.create_task_with_initial_turn` 在单一调用内完成：
  1. 创建顶层任务（标题由 input_text 派生，任务层不持有用户输入文本）。
  2. 创建首个 pending 轮次（input_text 归属轮次维度）。
  3. 返回 `(task, turn)` 二元组且两者关联一致。

装配不变量：
  - Settings.override 将全部 SQLite 库指向 tmp_path，不污染开发库。
  - init_storage() 建表 → initialize_service_dependencies() 装配 service 单例。
  - 先建 workspace（create_task 要求 workspace 存在），再触发组合方法。
  - teardown 关闭存储并复位全局单例与 Settings。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.config.configuration import build_agent_registry, set_agent_registry
from app.config.settings import Settings
from app.models import TaskRecord, TurnRecord
from app.service.depends import (
    close_service_dependencies,
    initialize_service_dependencies,
    reset_service_dependencies,
)
from app.service.task.task_service import TaskService
from app.service.task.workspace_service import WorkspaceService
from app.storage.store_engines import init_storage


@pytest.fixture
def storage_stack(tmp_path: Path):
    """装配指向 tmp_path 的真实存储与服务单例，测试结束复位全局状态。"""
    db_file = tmp_path / "app.sqlite3"
    log_db_file = tmp_path / "logs.sqlite3"
    checkpoint_file = tmp_path / "langgraph_checkpoints.sqlite"
    log_dir = tmp_path / "logs"
    Settings.override(
        DATABASE_FILE=db_file,
        LOG_DATABASE_FILE=log_db_file,
        CHECKPOINT_FILE=checkpoint_file,
        LOG_DIR=log_dir,
    )
    init_storage()
    initialize_service_dependencies()
    set_agent_registry(build_agent_registry())
    # 阶段 1.5 后 ``TurnService.create_turn`` 经 ``ModelResolverService.resolve``
    # 做 service 期预解析（设计 §6.4 两段式 ①），无 provider + model 行时抛
    # ``ModelNotConfiguredError``。本测试聚焦 task+turn 编排，不关心模型解析细节，
    # 但需为解析器播种一行可用模型，使触及 ``create_turn`` 的用例不再被 None 模型拒绝。
    _seed_minimal_provider_and_model()
    yield
    close_service_dependencies()
    reset_service_dependencies()
    Settings.load()


def _seed_minimal_provider_and_model() -> None:
    """为本组测试播种一行 deepseek 厂商 + 一行可用模型（不联网、不调 LLM）。

    参数:
        无。

    返回:
        无。

    异常:
        无（异常由调用方 fixture 捕获；正常路径下 DB 写入不会失败）。

    副作用:
        向 ``providers`` 与 ``models`` 表各插入一行；不输出 ``api_key`` 至日志。
    """

    from app.service.provider.provider_service import ProviderService
    from app.storage.crud.model_entry_crud import ModelEntryCrud

    provider = ProviderService().create_provider(
        name="DeepSeek 测试",
        provider_type="deepseek",
        api_key="sk-test-not-real",
    )
    ModelEntryCrud().create(
        provider_id=provider.provider_id,
        model_name="deepseek/deepseek-v4-flash",
        display_name="deepseek-v4-flash",
        max_context_window=1_000_000,
        supports_thinking=True,
    )


def _create_workspace() -> str:
    """创建一个测试工作区并返回其标识。"""
    workspace = WorkspaceService().create_workspace(
        name="ws_create_initial_turn",
        root_path="H:/ws_create_initial_turn",
    )
    return workspace.workspace_id


def test_creates_task_and_initial_pending_turn(storage_stack: None) -> None:
    """组合方法应创建任务与首个 pending 轮次，且两者关联一致。"""
    workspace_id = _create_workspace()
    task_service = TaskService()

    # 阶段 1.5 后必须显式传 ``model_name``，否则 ``create_turn`` 经
    # ``ModelResolverService.resolve`` 抛 ``ModelNotConfiguredError``
    # （``REASON_MODEL_NOT_SELECTED``）。此处复用 fixture 播种的 deepseek 模型。
    task, turn = task_service.create_task_with_initial_turn(
        workspace_id=workspace_id,
        agent_id="developer",
        input_text="实现登录接口",
        model_name="deepseek/deepseek-v4-flash",
    )

    assert isinstance(task, TaskRecord)
    assert isinstance(turn, TurnRecord)
    assert turn.task_id == task.task_id
    assert turn.status == "pending"
    assert turn.input_text == "实现登录接口"
    # 任务标题应由 input_text 派生，任务层不持有用户输入文本
    assert task.title == "实现登录接口"
    # 落库 model_name 应为解析后的 litellm 路由名（D11 时间线可追溯）
    assert turn.model_name == "deepseek/deepseek-v4-flash"


def test_requires_non_empty_input_text(storage_stack: None) -> None:
    """空白 input_text 应抛出 ValueError，不创建任何记录。"""
    workspace_id = _create_workspace()
    task_service = TaskService()

    with pytest.raises(ValueError):
        task_service.create_task_with_initial_turn(
            workspace_id=workspace_id,
            agent_id="developer",
            input_text="   ",
        )


def test_rejects_unknown_workspace(storage_stack: None) -> None:
    """不存在的工作区应触发外键完整性错误，不创建任何记录。"""
    task_service = TaskService()

    with pytest.raises(IntegrityError):  # 底层 create_task 经外键约束抛出
        task_service.create_task_with_initial_turn(
            workspace_id="nonexistent-workspace-id",
            agent_id="developer",
            input_text="任意输入",
        )


def test_rejects_unregistered_agent(storage_stack: None) -> None:
    """未注册的 agent_id 应抛出 ValueError，不创建任何记录。"""
    workspace_id = _create_workspace()
    task_service = TaskService()

    with pytest.raises(ValueError):
        task_service.create_task_with_initial_turn(
            workspace_id=workspace_id,
            agent_id="no-such-agent",
            input_text="任意输入",
        )


def test_compensates_orphan_task_on_turn_failure(storage_stack: None) -> None:
    """首轮次写入失败时，已创建的 task 应被补偿删除，不留孤儿任务。"""
    from app.service.task.task_service import TaskService as _TS

    workspace_id = _create_workspace()
    task_service = _TS()
    original_create = task_service._turn.create

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated turn persist failure")

    task_service._turn.create = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError):
            # 阶段 1.5 后 ``create_task_with_initial_turn`` 经
            # ``service_depends.get_turn_service().create_turn``（共享同一 ``TurnCrud``
            # 单例），故在此 mock ``task_service._turn.create``（CRUD 层 ``create``）
            # 仍能拦截到 ``create_turn`` 内的持久化调用。需先传合法 ``model_name``
            # 越过 ``ModelResolverService.resolve``，方能抵达被 mock 的 CRUD 调用。
            task_service.create_task_with_initial_turn(
                workspace_id=workspace_id,
                agent_id="developer",
                input_text="任意输入",
                model_name="deepseek/deepseek-v4-flash",
            )
    finally:
        task_service._turn.create = original_create  # type: ignore[assignment]

    # 补偿删除后，任务表应无残留
    from app.service.task.task_service import TaskService as _TS2

    remaining = _TS2().list_tasks_for_workspace(workspace_id)
    assert remaining == []
