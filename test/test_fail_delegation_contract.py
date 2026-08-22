"""``DelegationExecutor._fail_delegation`` 并发额度防泄漏契约的独立对抗性测试。

本文件独立于 ``apps/backend/tests/``，聚焦验证：
- (a) ``mark_failed`` 抛**任意**异常（RuntimeError / SQLAlchemyError / 自定义异常）时
     ``_fail_delegation`` 仍不抛出，且 ``mark_failed`` 仅被调用一次（不重试、不二次传播）。
- (b) ``delegation_id`` 为 ``None`` 或空串 ``""``（潜在误判点）时确实跳过 ``mark_failed``。
- (c) 位置参数顺序（delegation_id, error）正确，error 文案无截断。
- 额度释放契约真实性：用真实 ``DelegationService`` + 内存 ``DelegationCrud`` 桩，验证
     ``_fail_delegation`` 把 delegation 从 pending 推进到 failed 终态，且不再计入
     ``list_active_by_parent_turn`` 的 active 额度（而非仅断言 mock 调用）。
- 集成级：monkeypatch ``get_delegation_service`` 等为内存桩，驱动 ``execute`` 在
     IntegrityError 提前退出路径，验证真实 delegation 被推进到 failed（而非仅 mock 断言）。

不修改任何生产代码；仅新增本测试文件。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from sqlalchemy.exc import SQLAlchemyError

from app.core.agents.agent_profile import AgentProfile
from app.core.delegation.delegation_executor import DelegationExecutor
from app.models import TaskRecord, TurnRecord
from app.models.delegation_record import DelegationRecord
from app.service.delegation.delegation_service import ACTIVE_DELEGATION_STATUSES, DelegationService
from app.utils.datetime_utils import utc_now


# ---------------------------------------------------------------------------
# 内存 DelegationCrud 桩：复刻生产 update_status/get 语义（KeyError 当不存在）
# ---------------------------------------------------------------------------
class InMemoryDelegationCrud:
    """内存版 DelegationCrud，仅实现 ``_fail_delegation`` 验收所需的读写。

    行为对齐生产 ``update_status``：先 ``get``（不存在抛 KeyError），再更新状态/error。
    """

    def __init__(self) -> None:
        self._store: dict[str, DelegationRecord] = {}

    def seed_pending(self, delegation_id: str, parent_turn_id: str, task_id: str) -> None:
        now = utc_now()
        self._store[delegation_id] = DelegationRecord(
            delegation_id=delegation_id,
            task_id=task_id,
            parent_turn_id=parent_turn_id,
            child_turn_id="",
            child_task_id="",
            parent_agent_id="developer",
            child_agent_id="delegate_reviewer",
            delegation_type="reviewer",
            status="pending",
            prompt="p",
            summary="",
            error="",
            effective_tools=(),
            created_at=now,
            updated_at=now,
        )

    def get(self, delegation_id: str) -> DelegationRecord:
        if delegation_id not in self._store:
            raise KeyError(delegation_id)
        return self._store[delegation_id]

    def update_status(
        self,
        delegation_id: str,
        status: str,
        child_turn_id: str | None = None,
        child_task_id: str | None = None,
        summary: str | None = None,
        error: str | None = None,
    ) -> DelegationRecord:
        record = self.get(delegation_id)
        # DelegationRecord 为 frozen dataclass，必须用新实例替换（对齐生产 update 语义）
        updated = DelegationRecord(
            delegation_id=record.delegation_id,
            task_id=record.task_id,
            parent_turn_id=record.parent_turn_id,
            child_turn_id=child_turn_id if child_turn_id is not None else record.child_turn_id,
            child_task_id=child_task_id if child_task_id is not None else record.child_task_id,
            parent_agent_id=record.parent_agent_id,
            child_agent_id=record.child_agent_id,
            delegation_type=record.delegation_type,
            status=status,
            prompt=record.prompt,
            summary=summary if summary is not None else record.summary,
            error=error if error is not None else record.error,
            effective_tools=record.effective_tools,
            created_at=record.created_at,
            updated_at=utc_now(),
        )
        self._store[delegation_id] = updated
        return updated

    def list_by_parent_turn(self, parent_turn_id: str) -> list[DelegationRecord]:
        return [
            r for r in self._store.values() if r.parent_turn_id == parent_turn_id
        ]


class InMemoryRuntimeEventService:
    """内存版 RuntimeEventService 桩，记录发出的事件但不触碰数据库。"""

    def __init__(self) -> None:
        self.events: list = []

    def save_event(self, event):
        self.events.append(event)
        return event

    def publish_event(self, event) -> None:  # pragma: no cover - 仅记录
        self.events.append(event)


def _make_executor(policy=None) -> DelegationExecutor:
    """构造最小可用的 ``DelegationExecutor`` 桩（仅用于本测试）。"""

    parent_turn = TurnRecord(
        turn_id="parent-turn-1",
        task_id="task-1",
        input_text="parent",
        status="running",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        agent_id="developer",
    )
    parent_task = TaskRecord(
        task_id="task-1",
        workspace_id="ws-1",
        agent_id="developer",
        title="parent task",
        status="running",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    parent_profile = AgentProfile(
        agent_id="developer",
        role="developer",
        description="developer profile",
        allowed_tools=[],
    )
    return DelegationExecutor(
        child_runner=MagicMock(),
        parent_profile=parent_profile,
        parent_turn=parent_turn,
        parent_task=parent_task,
        policy=policy,
    )


def _real_delegation_service() -> tuple[DelegationService, InMemoryDelegationCrud]:
    """构造真实 ``DelegationService`` + 内存 crud 桩（用于契约真实性验证）。"""

    crud = InMemoryDelegationCrud()
    svc = DelegationService(
        delegation_crud=crud,
        runtime_event_service=InMemoryRuntimeEventService(),
    )
    return svc, crud


# ---------------------------------------------------------------------------
# (a) mark_failed 抛任意异常时不抛出且只调用一次
# ---------------------------------------------------------------------------
def test_fail_delegation_swallows_runtime_error_and_called_once() -> None:
    """mark_failed 抛 RuntimeError 时不二次抛出，且只调用一次（无重试）。"""

    executor = _make_executor()
    delegation_service = MagicMock()
    delegation_service.mark_failed.side_effect = RuntimeError("db down")

    executor._fail_delegation("deleg-1", delegation_service, None, "boom")

    assert delegation_service.mark_failed.call_count == 1


def test_fail_delegation_swallows_sqlalchemy_error_and_called_once() -> None:
    """mark_failed 抛 SQLAlchemyError（生产最可能异常）时不二次抛出，且只调用一次。"""

    executor = _make_executor()
    delegation_service = MagicMock()
    delegation_service.mark_failed.side_effect = SQLAlchemyError("connection lost")

    executor._fail_delegation("deleg-1", delegation_service, None, "boom")

    assert delegation_service.mark_failed.call_count == 1


def test_fail_delegation_swallows_arbitrary_exception_and_called_once() -> None:
    """mark_failed 抛任意自定义异常时不二次抛出，且只调用一次。"""

    class _WeirdError(Exception):
        pass

    executor = _make_executor()
    delegation_service = MagicMock()
    delegation_service.mark_failed.side_effect = _WeirdError("weird")

    executor._fail_delegation("deleg-1", delegation_service, None, "boom")

    assert delegation_service.mark_failed.call_count == 1


# ---------------------------------------------------------------------------
# (b) delegation_id 为 None / 空串时跳过
# ---------------------------------------------------------------------------
def test_fail_delegation_skips_when_none() -> None:
    """delegation_id 为 None（acquire 失败）时不调用 mark_failed。"""

    executor = _make_executor()
    delegation_service = MagicMock()

    executor._fail_delegation(None, delegation_service, None, "boom")

    delegation_service.mark_failed.assert_not_called()


def test_fail_delegation_skips_when_empty_string() -> None:
    """delegation_id 为空串时（潜在误判点：``not ''`` 也为真）同样应跳过 mark_failed。"""

    executor = _make_executor()
    delegation_service = MagicMock()

    executor._fail_delegation("", delegation_service, None, "boom")

    delegation_service.mark_failed.assert_not_called()


# ---------------------------------------------------------------------------
# (c) 位置参数顺序正确 + error 文案无截断
# ---------------------------------------------------------------------------
def test_fail_delegation_passes_position_args_and_full_error() -> None:
    """位置参数顺序为 (delegation_id, error)，且完整 error 文案无截断。"""

    executor = _make_executor()
    delegation_service = MagicMock()
    long_error = "x" * 5000  # 远超一般日志截断长度，验证文案完整透传

    executor._fail_delegation("deleg-1", delegation_service, None, long_error)

    args, _kwargs = delegation_service.mark_failed.call_args
    assert args[0] == "deleg-1"
    assert args[1] == long_error
    assert len(args[1]) == 5000


# ---------------------------------------------------------------------------
# 额度释放契约真实性（单元级）：pending -> failed，不再计入 active 额度
# ---------------------------------------------------------------------------
def test_fail_delegation_moves_pending_to_failed_terminal() -> None:
    """用真实 DelegationService：acquire 后的 pending delegation 被终态化为 failed。"""

    svc, crud = _real_delegation_service()
    crud.seed_pending("deleg-real", "parent-turn-1", "task-1")

    assert crud.get("deleg-real").status == "pending"

    executor = _make_executor()
    executor._fail_delegation("deleg-real", svc, None, "child task already exists")

    record = crud.get("deleg-real")
    assert record.status == "failed"
    assert record.error == "child task already exists"


def test_fail_delegation_releases_concurrency_slot() -> None:
    """终态化后 delegation 不再计入 parent turn 的 active 并发额度（防泄漏契约核心）。"""

    svc, crud = _real_delegation_service()
    crud.seed_pending("deleg-real", "parent-turn-1", "task-1")

    # acquire 后：占 1 个 active 额度
    assert len(svc.list_active_by_parent_turn("parent-turn-1")) == 1

    executor = _make_executor()
    executor._fail_delegation("deleg-real", svc, None, "boom")

    # 终态化后：active 额度归零，额度释放
    active = svc.list_active_by_parent_turn("parent-turn-1")
    assert active == []
    assert all(r.status not in ACTIVE_DELEGATION_STATUSES for r in active)


def test_fail_delegation_terminal_error_on_missing_delegation() -> None:
    """mark_failed 对不存在的 delegation 抛 KeyError（真实 service 行为），被收口吞掉。

    验证即使底层 update_status 抛 KeyError（记录已丢失），``_fail_delegation`` 仍不向外传播，
    避免掩盖根因——这也是生产环境可能遇到的异常分支。
    """

    svc, _crud = _real_delegation_service()
    executor = _make_executor()

    # 不应抛出（KeyError 被 _fail_delegation 吞掉并记 error 日志）
    executor._fail_delegation("ghost-deleg", svc, None, "boom")


# ---------------------------------------------------------------------------
# 额度释放契约真实性（集成级）：驱动 execute 在 IntegrityError 路径推进终态
# ---------------------------------------------------------------------------
def test_execute_integrity_error_path_finalizes_delegation_to_failed(monkeypatch) -> None:
    """execute 在 acquire 成功后遇 IntegrityError 时，真实 delegation 被推进到 failed 终态。

    这是并发重入冲突的提前退出路径：acquire 已占用 1 个并发额度，必须终态化以免泄漏。
    本用例用真实 DelegationService + 内存 crud，验证契约而非仅 mock 调用。
    """

    from app.tools.tool_models.delegate_task_args import DelegateTaskArgs
    import app.core.delegation.delegation_executor as exec_mod
    from app.service import depends as depends_mod

    svc, crud = _real_delegation_service()
    # 先种入一条 pending（模拟 acquire 成功后已占用额度）
    crud.seed_pending("deleg-exec", "parent-turn-1", "task-1")

    # execute 顶部通过模块级 `from app.service.depends import get_delegation_service,
    # get_turn_service`，而 get_task_service 在函数体内局部 `from import`。
    # 因此需同时 patch depends 模块（局部导入解析源模块属性）与 delegation_executor
    # 模块命名空间（模块级导入绑定了同名引用），确保两条导入路径都命中内存真实 service。
    monkeypatch.setattr(depends_mod, "get_delegation_service", lambda: svc)
    monkeypatch.setattr(depends_mod, "get_turn_service", lambda: MagicMock())
    monkeypatch.setattr(depends_mod, "get_task_service", lambda: MagicMock())
    monkeypatch.setattr(exec_mod, "get_delegation_service", lambda: svc)
    monkeypatch.setattr(exec_mod, "get_turn_service", lambda: MagicMock())

    # agent registry 解析：返回可解析的 child profile
    registry = MagicMock()
    registry.resolve.return_value = AgentProfile(
        agent_id="delegate_reviewer",
        role="reviewer",
        description="reviewer",
        allowed_tools=[],
        model_name="mock-model",
    )
    registry.child_agent_ids.return_value = ["delegate_reviewer"]
    monkeypatch.setattr(
        "app.core.delegation.delegation_executor.get_agent_registry", lambda: registry
    )

    # try_create_pending 返回已 acquire 的 delegation（复用上面种入的 id）
    from app.models.result.delegation_acquire_result import DelegationAcquireResult

    monkeypatch.setattr(
        svc,
        "try_create_pending",
        lambda **_: DelegationAcquireResult(acquired=True, delegation_id="deleg-exec", reason=""),
    )
    # create_child_task 抛 IntegrityError（并发重入冲突）
    from sqlalchemy.exc import IntegrityError

    task_svc = MagicMock()
    task_svc.create_child_task.side_effect = IntegrityError("dup", None, None)
    monkeypatch.setattr(depends_mod, "get_task_service", lambda: task_svc)

    # 使用允许一切的策略桩，确保走到 acquire 之后的 IntegrityError 路径
    policy = MagicMock()
    policy.resolve.return_value.allowed = True
    executor = _make_executor(policy=policy)
    args = DelegateTaskArgs(
        child_agent_id="delegate_reviewer",
        title="review",
        objective="do review",
        rules=[],
        references=[],
        expected_output="report",
    )
    exec_ctx = MagicMock()
    exec_ctx.runtime_dependencies.runtime_event_loop = None

    obs = executor.execute(args, exec_ctx)

    # 返回确定性 error observation（retryable=False）
    assert obs.status == "error"
    # 关键契约：真实 delegation 被推进到 failed，不再占 active 额度
    assert crud.get("deleg-exec").status == "failed"
    assert svc.list_active_by_parent_turn("parent-turn-1") == []
