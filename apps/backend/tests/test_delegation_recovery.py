"""delegation recovery tests."""

from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.config.settings import Settings
from app.models.delegation_record import DelegationRecord
from app.service.depends import get_delegation_service, reset_service_dependencies
from app.storage.crud.delegation_crud import DelegationCrud
from app.storage.store_engines import close_storage, init_storage
from app.utils.datetime_utils import utc_now


@pytest.fixture
def isolated_storage(tmp_path: Path) -> Iterator[None]:
    """为单个测试初始化隔离 SQLite 存储。

    参数:
        tmp_path: pytest 提供的临时目录。
    返回:
        已初始化的隔离存储上下文。
    异常:
        OSError: 当临时 SQLite 存储无法初始化时抛出。
    副作用:
        临时覆盖进程级存储路径，并在测试结束后关闭存储、清理 service 单例并恢复配置。
    """

    original_database_file = Settings.DATABASE_FILE
    original_log_database_file = Settings.LOG_DATABASE_FILE
    original_checkpoint_file = Settings.CHECKPOINT_FILE
    reset_service_dependencies()
    close_storage()
    Settings.override(
        DATABASE_FILE=tmp_path / "app.sqlite3",
        LOG_DATABASE_FILE=tmp_path / "logs.sqlite3",
        CHECKPOINT_FILE=tmp_path / "checkpoints.sqlite3",
    )
    init_storage()
    try:
        yield
    finally:
        reset_service_dependencies()
        close_storage()
        Settings.override(
            DATABASE_FILE=original_database_file,
            LOG_DATABASE_FILE=original_log_database_file,
            CHECKPOINT_FILE=original_checkpoint_file,
        )


def test_recovery_marks_pending_and_running_delegations_failed(isolated_storage: None) -> None:
    """验证恢复审计只把中断委派标记为 failed 并持久化原因。

    参数:
        isolated_storage: 已初始化的隔离 SQLite 存储 fixture。
    返回:
        无。
    异常:
        AssertionError: 当 active 委派未落定为 failed 或终态委派被误改时由 pytest 抛出。
    副作用:
        向隔离数据库写入多条 delegation 记录，并通过 DelegationService 执行恢复审计。
    """

    crud = DelegationCrud()
    _create_delegation(crud, "delegation_pending", "pending")
    _create_delegation(crud, "delegation_running", "running", child_turn_id="turn_child")
    _create_delegation(crud, "delegation_completed", "completed", summary="done")
    _create_delegation(crud, "delegation_failed", "failed", error="already_failed")
    _create_delegation(crud, "delegation_cancelled", "cancelled", error="user_cancelled")
    service = get_delegation_service()

    recovered_count = service.mark_interrupted_delegations_failed("runtime_restarted")

    assert recovered_count == 2
    by_id = {item.delegation_id: item for item in service.list_by_parent_turn("turn_parent")}
    assert by_id["delegation_pending"].status == "failed"
    assert by_id["delegation_pending"].error == "runtime_restarted"
    assert by_id["delegation_running"].status == "failed"
    assert by_id["delegation_running"].error == "runtime_restarted"
    assert by_id["delegation_completed"].status == "completed"
    assert by_id["delegation_completed"].summary == "done"
    assert by_id["delegation_failed"].status == "failed"
    assert by_id["delegation_failed"].error == "already_failed"
    assert by_id["delegation_cancelled"].status == "cancelled"
    assert by_id["delegation_cancelled"].error == "user_cancelled"


async def test_lifespan_runs_delegation_recovery_before_runtime_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证应用启动在 runtime/tool system 启动前执行委派恢复审计。

    参数:
        monkeypatch: pytest 提供的属性替换 fixture。
    返回:
        无。
    异常:
        AssertionError: 当启动顺序或恢复原因不符合预期时由 pytest 抛出。
    副作用:
        临时替换 app lifespan 的外部启动协作者，避免真实启动存储、CodeGraph 和 runtime。
    """

    import app.app as app_module

    calls: list[tuple[str, object | None]] = []
    fake_delegation_service = _FakeDelegationService(calls)

    monkeypatch.setattr(app_module.Settings, "load", lambda: calls.append(("settings", None)))
    monkeypatch.setattr(
        app_module,
        "initialize_service_dependencies",
        lambda: calls.append(("initialize_services", None)),
    )
    monkeypatch.setattr(
        app_module,
        "install_logging_for_current_process",
        lambda **_kwargs: calls.append(("install_logging", None)),
    )
    monkeypatch.setattr(
        app_module,
        "get_delegation_service",
        lambda: fake_delegation_service,
        raising=False,
    )
    monkeypatch.setattr(
        app_module,
        "_start_codegraph_kernel",
        _fake_start_codegraph_kernel(calls),
    )
    monkeypatch.setattr(
        app_module.ToolSystem,
        "build_tool_system",
        lambda _client: calls.append(("build_tool_system", None)) or object(),
    )
    monkeypatch.setattr(
        app_module,
        "set_tool_system",
        lambda _tool_system: calls.append(("set_tool_system", None)),
    )
    monkeypatch.setattr(
        app_module,
        "set_agent_registry",
        lambda _registry: calls.append(("set_agent_registry", None)),
    )
    monkeypatch.setattr(
        app_module,
        "build_agent_registry",
        lambda: calls.append(("build_agent_registry", None)) or object(),
    )
    monkeypatch.setattr(
        app_module,
        "set_runtime",
        lambda _runtime: calls.append(("set_runtime", None)),
    )
    monkeypatch.setattr(
        app_module,
        "AgentRuntime",
        lambda: calls.append(("agent_runtime", None)) or object(),
    )
    monkeypatch.setattr(
        app_module.HookInterceptor,
        "safe_fire",
        lambda _context: calls.append(("hook", None)),
    )
    monkeypatch.setattr(app_module, "_mark_boot_ready", lambda: calls.append(("ready", None)))
    monkeypatch.setattr(
        app_module,
        "close_service_dependencies",
        lambda: calls.append(("close_services", None)),
    )
    monkeypatch.setattr(
        app_module,
        "flush_langfuse",
        lambda: calls.append(("flush_langfuse", None)),
    )
    monkeypatch.setattr(
        app_module,
        "_mark_boot_stopped",
        lambda: calls.append(("stopped", None)),
    )

    async with app_module.lifespan(FastAPI()):
        pass

    assert ("recover_delegations", "runtime_restarted") in calls
    assert calls.index(("install_logging", None)) < calls.index(
        ("recover_delegations", "runtime_restarted")
    )
    assert calls.index(("recover_delegations", "runtime_restarted")) < calls.index(
        ("start_codegraph", None)
    )
    assert calls.index(("recover_delegations", "runtime_restarted")) < calls.index(
        ("build_tool_system", None)
    )
    assert calls.index(("recover_delegations", "runtime_restarted")) < calls.index(
        ("set_runtime", None)
    )


def _create_delegation(
    crud: DelegationCrud,
    delegation_id: str,
    status: str,
    child_turn_id: str = "",
    summary: str = "",
    error: str = "",
) -> None:
    """向隔离存储写入一条 delegation 测试记录。

    参数:
        crud: 负责写入 delegation 表的 CRUD 协作者。
        delegation_id: 测试记录标识。
        status: 测试记录状态。
        child_turn_id: 可选 child turn 标识。
        summary: 可选成功摘要。
        error: 可选错误原因。
    返回:
        无。
    异常:
        sqlalchemy.exc.SQLAlchemyError: 当 delegation 记录写入失败时抛出。
    副作用:
        向当前隔离 SQLite 数据库写入一条 delegation 记录。
    """

    now = utc_now()
    crud.create(
        DelegationRecord(
            delegation_id=delegation_id,
            task_id="task_1",
            parent_turn_id="turn_parent",
            child_turn_id=child_turn_id,
            parent_agent_id="developer",
            child_agent_id="delegate_reviewer",
            delegation_type="review",
            status=status,
            prompt="review",
            summary=summary,
            error=error,
            effective_tools=("read_file",),
            created_at=now,
            updated_at=now,
        )
    )


class _FakeDelegationService:
    """记录启动恢复审计调用的 fake delegation service。"""

    def __init__(self, calls: list[tuple[str, object | None]]) -> None:
        """初始化 fake service。

        参数:
            calls: 共享调用记录列表。
        返回:
            无。
        异常:
            无。
        副作用:
            保存共享调用记录列表引用。
        """

        self._calls = calls

    def mark_interrupted_delegations_failed(self, reason: str) -> int:
        """记录恢复审计调用并返回固定数量。

        参数:
            reason: 启动恢复审计使用的失败原因。
        返回:
            固定返回 3，表示 fake 中有三条记录被落定。
        异常:
            无。
        副作用:
            向共享调用记录列表追加恢复审计调用。
        """

        self._calls.append(("recover_delegations", reason))
        return 3


def _fake_start_codegraph_kernel(
    calls: list[tuple[str, object | None]],
) -> Callable[[], Awaitable[None]]:
    """构造记录 CodeGraph 启动调用的异步 fake。

    参数:
        calls: 共享调用记录列表。
    返回:
        可替换 ``_start_codegraph_kernel`` 的异步函数。
    异常:
        无。
    副作用:
        无；返回的 fake 被调用时会写入调用记录。
    """

    async def fake_start_codegraph_kernel() -> None:
        """记录 CodeGraph 启动调用。

        参数:
            无。
        返回:
            无。
        异常:
            无。
        副作用:
            向共享调用记录列表追加 CodeGraph 启动调用。
        """

        calls.append(("start_codegraph", None))
        return None

    return fake_start_codegraph_kernel
