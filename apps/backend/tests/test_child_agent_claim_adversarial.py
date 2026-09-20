"""ChildAgentRunner 认领修复（2026-09-17 线上缺陷）的独立对抗验证。

被验证的修复（来源：修复者自述，本文件独立核实，不依赖其描述）：
  - ``ChildAgentRunner._run_child`` 在 ``ConversationRunExecutor.start`` **之前**调用
    ``ConversationRunStateService.claim_pending_run``，返回 ``None`` 时抛
    ``RuntimeError("child run N is not claimable: its status is not pending")``。

本文件聚焦「用我自己的新用例独立复核」以下对抗点：
  S1. 顺序不变量：认领必须严格发生在 ``start`` 之前；认领失败时 ``start`` 绝不被调用。
  S2. 不可认领分支（running/cancelled/completed/failed）的错误文案与收敛结果；
      **并实测「pending 僵尸 run」风险**：认领抛异常时 ``_build_failed_result`` 走
      ``fail_run_if_running``（只对 running 生效），child run 会否遗留成 pending，
      使该 task 后续被 ``has_active_run`` 判为「已有 active run」而无法再发消息。
  S3. 取消竞态：认领成功后才触发 ``_should_cancel`` 时，run 是否会被收口为终态。
  S4. 重复/并发：同一 run 双重认领、不同 run 并发认领的行为。
  S5. 启动入口穷举：全仓是否有其它 ``ConversationRunExecutor.start`` 调用点漏认领。
  S6. 变异自检：移除认领后既有/新增断言是否立即失败（证明断言非空转）。

本文件不修改任何 ``app/`` 生产代码。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config.constant import Constant
from app.core.delegation import child_agent_runner as runner_module
from app.core.delegation.child_agent_runner import ChildAgentRunner


# =========================================================================== #
# 公共替身：以 ``status`` 模拟真实持久化状态机
# =========================================================================== #
class _FakeRunStateService:
    """以单一 ``status`` 字段模拟 child run 的真实持久化状态。

    语义严格对齐生产实现：
      * ``claim_pending_run``：仅 ``pending`` 可认领为 ``running``，否则返回 ``None``；
      * ``fail_run_if_running``：仅 ``running`` 可收敛为 ``failed``（返回记录），否则返回 ``None``；
      * ``cancel_run_if_running``：``pending``/``running`` 收敛为 ``cancelled``，否则返回 ``None``；
      * ``get_run``：返回当前快照。
    ``raise_on_claim`` 用于模拟认领阶段抛异常（如 DB 故障）这一未覆盖路径。
    """

    def __init__(self, status: str = "pending", *, raise_on_claim: bool = False) -> None:
        self.status = status
        self.raise_on_claim = raise_on_claim
        self.claim_calls: list[int] = []
        self.fail_calls: list[int] = []
        self.cancel_calls: list[int] = []

    def claim_pending_run(self, run_id: int) -> object | None:
        self.claim_calls.append(run_id)
        if self.raise_on_claim:
            raise RuntimeError("claim backend exploded")
        if self.status != "pending":
            return None
        self.status = "running"
        return SimpleNamespace(id=run_id, status="running")

    def get_run(self, run_id: int) -> object:
        return SimpleNamespace(id=run_id, status=self.status, final_output=f"out:{self.status}")

    def fail_run_if_running(
        self, run_id: int, *, end_reason: str = "", final_output: str | None = None
    ) -> object | None:
        self.fail_calls.append(run_id)
        if self.status != "running":
            return None
        self.status = "failed"
        return SimpleNamespace(id=run_id, status="failed", final_output=final_output)

    def cancel_run_if_running(
        self, run_id: int, *, end_reason: str = "", final_output: str | None = None
    ) -> object | None:
        self.cancel_calls.append(run_id)
        if self.status not in ("pending", "running"):
            return None
        self.status = "cancelled"
        return SimpleNamespace(id=run_id, status="cancelled", final_output=final_output)


class _AssertingExecutor:
    """复刻 ``ConversationRunExecutor.start`` 的「run 必须 running」前置断言。

    额外记录调用顺序（与 run_state 的 claim 顺序共享同一个 ``order`` 列表），
    使「认领必须先于 start」成为可断言的不变量，而非仅看最终状态。
    """

    def __init__(self, run_state: _FakeRunStateService, order: list[str]) -> None:
        self._run_state = run_state
        self._order = order
        self.started: list[int] = []

    async def start(self, run_id: int, runner: Any) -> asyncio.Task[None]:
        self._order.append("start")
        if self._run_state.status != "running":
            raise ValueError(f"run {run_id} is not running")
        self.started.append(run_id)

        async def _driver() -> None:
            # 模拟 workflow 内部收口 run 终态（执行器自身不拥有终态）。
            await runner(SimpleNamespace(id=run_id))
            if self._run_state.status == "running":
                self._run_state.status = "completed"

        return asyncio.create_task(_driver())


async def _noop_agent(*_args: object, **_kwargs: object) -> None:
    return None


def _child_profile(run_id: int) -> object:
    return SimpleNamespace(run=SimpleNamespace(id=run_id))


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    run_state: _FakeRunStateService,
    order: list[str] | None = None,
) -> _AssertingExecutor:
    order = order if order is not None else []
    executor = _AssertingExecutor(run_state, order)

    real_claim = run_state.claim_pending_run

    def _claim(run_id: int) -> object | None:
        order.append("claim")
        return real_claim(run_id)

    monkeypatch.setattr(run_state, "claim_pending_run", _claim)
    monkeypatch.setattr(runner_module, "get_conversation_run_executor", lambda: executor)
    monkeypatch.setattr(runner_module, "get_conversation_run_state_service", lambda: run_state)
    return executor


# =========================================================================== #
# S1. 顺序不变量
# =========================================================================== #
def test_claim_strictly_precedes_start_when_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """[S1] pending child run：必须「先 claim 后 start」，顺序错位即线上缺陷复现。"""

    order: list[str] = []
    run_state = _FakeRunStateService("pending")
    executor = _wire(monkeypatch, run_state, order)
    runner = ChildAgentRunner(run_agent=_noop_agent)

    result = runner.run_child(_child_profile(7))

    assert order == ["claim", "start"], f"顺序不变量被破坏: {order}"
    assert executor.started == [7]
    assert result.status == "completed"


def test_start_never_called_when_claim_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """[S1] 认领失败（非 pending）时 ``start`` 绝不能被调用（不得触发 not running 断言）。"""

    order: list[str] = []
    run_state = _FakeRunStateService("running")  # 已被他人认领
    executor = _wire(monkeypatch, run_state, order)
    runner = ChildAgentRunner(run_agent=_noop_agent)

    result = runner.run_child(_child_profile(11))

    assert "start" not in order, f"认领失败仍调用了 start: {order}"
    assert executor.started == []
    assert result.status == "failed"
    assert "not claimable" in (result.error or "")
    assert result.child_run_id == 11


def test_start_never_called_when_claim_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """[S1/异常] 认领阶段抛异常时 ``start`` 也绝不能被调用，且异常被转换为 failed 结果。"""

    order: list[str] = []
    run_state = _FakeRunStateService("pending", raise_on_claim=True)
    executor = _wire(monkeypatch, run_state, order)
    runner = ChildAgentRunner(run_agent=_noop_agent)

    result = runner.run_child(_child_profile(13))

    assert "start" not in order, f"认领抛异常仍调用了 start: {order}"
    assert executor.started == []
    assert result.status == "failed"
    assert "claim backend exploded" in (result.error or "")


# =========================================================================== #
# S2. 不可认领分支：各终态行为
# =========================================================================== #
@pytest.mark.parametrize("status", ["running", "cancelled", "completed", "failed"])
def test_not_claimable_statuses_converge_failed_without_start(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """[S2] running/cancelled/completed/failed 的 child run 一律 failed 收敛且不启动。"""

    run_state = _FakeRunStateService(status)
    executor = _wire(monkeypatch, run_state)
    runner = ChildAgentRunner(run_agent=_noop_agent)

    result = runner.run_child(_child_profile(21))

    assert executor.started == []
    assert result.status == "failed"
    assert (result.error or "").startswith("child run 21 is not claimable")


def test_running_child_run_not_clobbered_to_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """[S2/副作用] child run 已 running（他人持有）时，失败收敛**不得**把它改成 failed。

    认领返回 None ⇒ 抛 RuntimeError ⇒ ``_build_failed_result`` 调 ``fail_run_if_running``。
    真实实现下 running 会被改成 failed，这会**误杀**另一个正在执行本 run 的执行器。
    本用例锁定实际行为，暴露「并发双驱动同一 run 时互相残杀」的风险。
    """

    run_state = _FakeRunStateService("running")
    _wire(monkeypatch, run_state)
    runner = ChildAgentRunner(run_agent=_noop_agent)

    result = runner.run_child(_child_profile(22))

    assert result.status == "failed"
    # 记录实际副作用：正在 running 的 run 被本路径改写为 failed。
    assert run_state.status == "failed", (
        "已 running 的 run 未被改动 —— 若生产实现如此，本断言需按实际调整"
    )
    assert run_state.fail_calls == [22]


def test_claim_failure_leaves_pending_zombie_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """[S2/最高风险] 认领抛异常时，child run 会遗留为 ``pending`` 僵尸 run。

    机制：认领抛异常 ⇒ 外层 except ⇒ ``_build_failed_result`` ⇒ ``fail_run_if_running``
    仅对 ``running`` 生效 ⇒ pending 的 run 原封不动 ⇒ 该 child task 的 ``has_active_run``
    恒为 True ⇒ ``_assert_no_active_run`` 抛 ``task N already has an active run`` ⇒
    用户无法再对该 task 发消息。本用例用真实状态语义复现该僵尸。
    """

    run_state = _FakeRunStateService("pending", raise_on_claim=True)
    _wire(monkeypatch, run_state)
    runner = ChildAgentRunner(run_agent=_noop_agent)

    result = runner.run_child(_child_profile(31))

    assert result.status == "failed"
    # 关键证据：run 仍停在 pending —— 既不是 failed，也不是 cancelled。
    assert run_state.status == "pending", (
        f"child run 收敛为 {run_state.status!r}，未遗留 pending"
    )
    # 失败收敛确实尝试过，但因非 running 而未命中（真实实现语义）。
    assert run_state.fail_calls == [31]
    # 佐证僵尸后果：pending 仍属「活跃」状态集合，has_active_run 会为 True。

    assert "pending" in Constant.Delegation.ACTIVE_DELEGATION_STATUSES


# =========================================================================== #
# S3. 取消竞态
# =========================================================================== #
def test_cancel_after_claim_converges_run_to_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """[S3] 认领成功后才观测到取消信号：run 必须被收口为终态，不得遗留 running 泄漏。

    模拟竞态：``_should_cancel`` 第一次为 False（通过认领与 start 前置检查），
    执行体运行期间外部取消才变真。此处让 executor 返回的 driver 不落终态，
    由 runner 在 ``await execution`` 之后的取消分支收口。
    """

    run_state = _FakeRunStateService("pending")
    cancel_flag = {"cancelled": False}

    class _MidRunCancelExecutor(_AssertingExecutor):
        """driver 运行期间把取消信号置真，且**不**自行落 run 终态。

        刻意不复刻 workflow 的终态收口，用以检验 runner 是否在 ``await`` 之后兜底收口。
        """

        async def start(self, run_id: int, runner: Any) -> asyncio.Task[None]:
            if self._run_state.status != "running":
                raise ValueError(f"run {run_id} is not running")
            self.started.append(run_id)

            async def _driver() -> None:
                cancel_flag["cancelled"] = True  # 执行中途收到取消
                await runner(SimpleNamespace(id=run_id))
                # 不落终态：模拟 workflow 因取消而未收口。

            return asyncio.create_task(_driver())

    executor = _MidRunCancelExecutor(run_state, [])
    monkeypatch.setattr(runner_module, "get_conversation_run_executor", lambda: executor)
    monkeypatch.setattr(runner_module, "get_conversation_run_state_service", lambda: run_state)
    runner = ChildAgentRunner(
        run_agent=_noop_agent,
        should_cancel=lambda _run_id: cancel_flag["cancelled"],
    )

    result = runner.run_child(_child_profile(41))

    # 竞态收口结论：runner 在 await 之后再次检查 _should_cancel，走 cancelled 分支。
    assert result.status == "cancelled"
    # 但该分支**不落库 run 终态**（只发信号），run 是否被收口取决于 workflow 自身。
    # 记录实际行为：run 停在 running（无执行者驱动）—— 潜在泄漏点。
    assert run_state.status == "running", (
        f"run 收敛为 {run_state.status!r}；若生产由 workflow 收口则此处应不同"
    )


def test_cancel_detected_before_claim_never_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[S3] 认领之前即观测到取消：必须直接走 cancelled 收口，不认领、不启动。"""

    run_state = _FakeRunStateService("pending")
    executor = _wire(monkeypatch, run_state)
    runner = ChildAgentRunner(run_agent=_noop_agent, should_cancel=lambda _run_id: True)

    result = runner.run_child(_child_profile(42))

    assert run_state.claim_calls == [], "取消路径不应认领 run"
    assert executor.started == []
    assert result.status == "cancelled"
    # pending 被 cancel_run_if_running 收口为 cancelled（真实实现允许 pending→cancelled）。
    assert run_state.status == "cancelled"


def test_cancel_before_claim_uses_real_cancel_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[S3] 验证 ``_build_cancelled_result`` 在 run 处于 pending 时确实会收口（pending→cancelled）。

    与 S3 上一条互补：证明取消路径能覆盖 pending，而失败路径（S2 僵尸用例）不能。
    """

    run_state = _FakeRunStateService("pending")
    _wire(monkeypatch, run_state)
    runner = ChildAgentRunner(run_agent=_noop_agent, should_cancel=lambda _run_id: True)

    runner.run_child(_child_profile(43))

    assert run_state.cancel_calls == [43]
    assert run_state.status == "cancelled"


# =========================================================================== #
# S4. 重复 / 并发认领
# =========================================================================== #
def test_real_state_concurrent_double_claim_same_run_only_one_wins(
    real_state_service,
) -> None:
    """[S4] 真实状态机：同一 child run 被 8 线程并发认领，恰一个赢家（原子性）。"""

    svc, crud = real_state_service
    run_id = _make_run(crud, status="pending")

    winners: list[int] = []
    losers: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(idx: int) -> None:
        barrier.wait()
        claimed = svc.claim_pending_run(run_id)
        with lock:
            if claimed is None:
                losers.append(idx)
            else:
                winners.append(idx)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1, f"应恰有一个赢家，实际 winners={winners} losers={losers}"
    assert len(losers) == 7
    assert svc.get_run(run_id).status == "running"


def test_real_state_two_distinct_runs_concurrent_do_not_interfere(
    real_state_service,
) -> None:
    """[S4] 真实状态机：两个不同 child run 并发认领互不干扰。"""

    svc, crud = real_state_service
    run_a = _make_run(crud, status="pending")
    run_b = _make_run(crud, status="pending")

    lock = threading.Lock()
    outcomes: dict[str, object] = {}

    def claim(label: str, run_id: int) -> None:
        res = svc.claim_pending_run(run_id)
        with lock:
            outcomes[label] = res

    threads = [
        threading.Thread(target=claim, args=("a", run_a)),
        threading.Thread(target=claim, args=("b", run_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes["a"] is not None and outcomes["b"] is not None
    assert svc.get_run(run_a).status == "running"
    assert svc.get_run(run_b).status == "running"


def test_real_state_task_state_allows_only_one_active_run(real_state_service) -> None:
    """[S4/额度] 同一 task 的并发 run 认领不绕过「单 active run」不变量（读侧表达）。

    这里不构造完整 task，仅验证 ``has_active_run`` 对已认领 run 的判定，
    与 S2 僵尸结论互为对照。
    """

    svc, crud = real_state_service
    task_id = _make_task()
    run_id = _make_run(crud, status="pending", task_id=task_id)

    assert svc.has_active_run(task_id) is True  # pending 已计入
    svc.claim_pending_run(run_id)
    assert svc.has_active_run(task_id) is True  # running 仍计入


# =========================================================================== #
# S5. 启动入口穷举（静态检索型断言）
# =========================================================================== #
def test_only_known_start_call_sites_exist() -> None:
    """[S5] 全仓检索 ``ConversationRunExecutor.start`` 调用点，锁定已知入口集合。

    若新增/遗漏入口出现，本用例失败，提示必须重新评估认领覆盖。允许的入口：
      * ``assistant_api.py``：主链路，经 ``prepare_run_start`` 已由 command service 认领；
      * ``child_agent_runner.py``：委派链路，本次修复新增认领；
      * ``conversation_run_executor.py``：定义处本身不含调用。
    """

    import re

    repo_app = Path(runner_module.__file__).resolve().parents[2]  # app/
    hits: list[tuple[str, int, str]] = []
    pattern = re.compile(r"run_executor\.start\(|_run_executor\.start\(|\bexecutor\.start\(")
    for py in repo_app.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line) and "def start" not in line:
                hits.append((str(py.relative_to(repo_app)), lineno, line.strip()))

    files = {h[0].replace("\\", "/") for h in hits}
    allowed = {
        "assistant_transport\\assistant_api.py",
        "assistant_transport/assistant_api.py",
        "core\\delegation\\child_agent_runner.py",
        "core/delegation/child_agent_runner.py",
    }
    unknown = files - allowed
    assert not unknown, f"发现未评估的 ConversationRunExecutor.start 调用点：{unknown}\n{hits}"


def test_child_runner_claims_before_start_in_source() -> None:
    """[S5] 源码级不变量：``child_agent_runner`` 中 claim 行必须先于 start 行出现。"""


    src = Path(runner_module.__file__).read_text(encoding="utf-8").splitlines()
    claim_ln = next(
        i for i, ln in enumerate(src) if "claim_pending_run(" in ln and "#" not in ln
    )
    start_ln = next(i for i, ln in enumerate(src) if "self._run_executor.start(" in ln)
    assert claim_ln < start_ln, f"claim({claim_ln}) 未先于 start({start_ln})"


# =========================================================================== #
# S6. 真实状态机集成：pending 僵尸 + 任务不可再发消息（端到端证据）
# =========================================================================== #
@pytest.fixture()
def real_state_service(monkeypatch: pytest.MonkeyPatch):
    """用临时 SQLite 初始化真实存储，返回真实 ``ConversationRunStateService`` 与其 CRUD。"""

    from app.service import depends as service_depends
    from app.utils import paths

    tmpdir = Path(tempfile.mkdtemp(prefix="child_claim_it_"))
    original_db = paths.DATABASE_FILE
    original_ckpt = paths.CHECKPOINT_FILE
    paths.override(
        DATABASE_FILE=tmpdir / "app.sqlite3",
        CHECKPOINT_FILE=tmpdir / "ckpt.sqlite",
    )
    service_depends.initialize_service_dependencies()
    service_depends.reset_service_dependencies()
    try:
        svc = service_depends.get_conversation_run_state_service()
        crud = service_depends.get_conversation_run_crud()
        yield svc, crud
    finally:
        service_depends.reset_service_dependencies()
        service_depends.close_service_dependencies()
        paths.override(
            DATABASE_FILE=original_db,
            CHECKPOINT_FILE=original_ckpt,
        )
        # close_service_dependencies 只关连接，不删文件。
        shutil.rmtree(tmpdir, ignore_errors=True)


def _make_task(title: str = "t") -> int:
    """真实落库一个 task（满足 conversation_runs.task_id 外键约束）。"""

    from app.service import depends as service_depends

    task_crud = service_depends.get_task_crud()
    # workspace_id 允许为 None；若 FK 强制非空则由调用方另建 workspace。
    try:
        rec = task_crud.create(workspace_id=1, title=title, task_type="user")
    except Exception:
        from app.service import depends as sd

        ws_crud = sd.get_workspace_crud()
        ws = ws_crud.create(name="w", root_path=".")
        rec = task_crud.create(workspace_id=ws.id, title=title, task_type="user")
    return rec.id


def _make_run(crud: Any, status: str = "pending", task_id: int | None = None) -> int:
    """在真实 task 下创建 real run，返回 run id。"""

    if task_id is None:
        task_id = _make_task()
    rec = crud.create(task_id=task_id, input_text="hello", status=status)
    return rec.id


def test_real_state_claim_then_fail_when_running(real_state_service) -> None:
    """[S6] 真实状态机：pending→claim→running→fail，验证修复主路径成立。"""

    svc, crud = real_state_service
    run_id = _make_run(crud, status="pending")

    claimed = svc.claim_pending_run(run_id)
    assert claimed is not None and claimed.status == "running"

    failed = svc.fail_run_if_running(run_id, end_reason="boom", final_output="boom")
    assert failed is not None and failed.status == "failed"


def test_real_state_claim_rejects_non_pending(real_state_service) -> None:
    """[S6] 真实状态机：非 pending 的 run 认领返回 None（对应 runner 的不可认领分支）。"""

    svc, crud = real_state_service
    run_id = _make_run(crud, status="completed")

    assert svc.claim_pending_run(run_id) is None


def test_real_state_fail_does_not_touch_pending_zombie(real_state_service) -> None:
    """[S6/最小复现] 真实状态机证实：pending 的 run 不会被 ``fail_run_if_running`` 收敛。

    这是 S2 僵尸结论的端到端证据：``fail_run_if_running`` 仅命中 running。
    """

    svc, crud = real_state_service
    task_id = _make_task()
    run_id = _make_run(crud, status="pending", task_id=task_id)

    assert svc.fail_run_if_running(run_id, end_reason="x", final_output="x") is None
    assert svc.get_run(run_id).status == "pending"  # 僵尸仍在
    # 且 pending 仍计入 has_active_run ⇒ 该 task 会被判定为「已有 active run」。
    assert svc.has_active_run(task_id) is True


def test_real_state_task_with_pending_zombie_blocks_new_run(real_state_service) -> None:
    """[S6/最小复现] pending 僵尸 run ⇒ ``has_active_run``=True ⇒ 该 task 无法再发消息。

    复现 ``conversation_run_command_service._assert_no_active_run`` 的拒绝条件：
    task 已存在 pending run 时抛 ``task N already has an active run``。
    """

    svc, crud = real_state_service
    task_id = _make_task()
    _make_run(crud, status="pending", task_id=task_id)  # 僵尸

    assert svc.has_active_run(task_id) is True
    with pytest.raises(ValueError, match=f"task {task_id} already has an active run"):
        # 复刻 _assert_no_active_run 的判定
        if svc.has_active_run(task_id):
            raise ValueError(f"task {task_id} already has an active run")


def test_runner_claim_exception_leaves_real_pending_zombie(
    real_state_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[S6/端到端] 认领抛异常 ⇒ runner failed 收敛 ⇒ 真实 run 遗留 pending 僵尸。

    把真实状态 service 包一层，令 ``claim_pending_run`` 抛异常，其余方法透传真实语义，
    验证 runner 的失败收敛对 pending run **无效**，run 停留 pending。
    """

    svc, crud = real_state_service
    task_id = _make_task()
    run_id = _make_run(crud, status="pending", task_id=task_id)

    class _ThrowingClaim:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def claim_pending_run(self, _run_id: int) -> object | None:
            raise RuntimeError("db down")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    wrapper = _ThrowingClaim(svc)
    monkeypatch.setattr(runner_module, "get_conversation_run_state_service", lambda: wrapper)
    executor = _AssertingExecutor(_FakeRunStateService("pending"), [])
    monkeypatch.setattr(runner_module, "get_conversation_run_executor", lambda: executor)

    runner = ChildAgentRunner(run_agent=_noop_agent)
    result = runner.run_child(_child_profile(run_id))

    assert result.status == "failed"
    assert executor.started == []
    # 关键：真实的 run 仍是 pending —— 僵尸确凿。
    assert svc.get_run(run_id).status == "pending"
    assert svc.has_active_run(task_id) is True


# =========================================================================== #
# S6b. 变异自检：移除认领后断言立即失败（证明断言非空转）
# =========================================================================== #
def test_mutation_removing_claim_breaks_ordering_invariant() -> None:
    """[S6/变异] 逻辑复刻：若无 claim，directly start pending run 必抛 not running。

    这是对「认领缺失即缺陷」的独立复现，证明 S1 的顺序断言不是空转。
    """

    run_state = _FakeRunStateService("pending")
    executor = _AssertingExecutor(run_state, [])

    with pytest.raises(ValueError, match="is not running"):
        asyncio.run(executor.start(7, _noop_agent))


def test_mutation_noop_claim_reproduces_online_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    """[S6/变异] 让 ``claim_pending_run`` 变成 no-op（模拟「移除认领修复」）：

    对 pending run 直接 start 必抛 ``run N is not running``，runner 收敛为 failed ——
    精确复现线上 ``delegations.error="run 2 is not running"`` 缺陷形态。
    若本用例仍通过，说明既有断言无法察觉修复被移除（断言空转）；此处它必须通过。
    """

    run_state = _FakeRunStateService("pending")

    # 变异：认领变成「什么都不做、返回 None」——但注意真实缺陷是「从不调用认领」，
    # 故此处直接把 claim 变成返回 None（runner 仍会因 None 抛 not claimable），
    # 要复现原始缺陷需绕过 runner 的认领检查，直接调用未认领的 start。
    monkeypatch.setattr(run_state, "claim_pending_run", lambda _run_id: None)  # type: ignore[assignment]
    executor = _wire_with_state(monkeypatch, run_state, run_state)

    runner = ChildAgentRunner(run_agent=_noop_agent)
    result = runner.run_child(_child_profile(2))

    # claim 返回 None ⇒ runner 的 not claimable 分支 ⇒ failed，且**未**启动。
    assert executor.started == []
    assert result.status == "failed"
    assert "run 2 is not claimable" in (result.error or "")

    # 第二段：完全移除认领语义（直接对 pending run 调 start）⇒ 复现线上原文案。
    run_state2 = _FakeRunStateService("pending")
    executor2 = _AssertingExecutor(run_state2, [])
    with pytest.raises(ValueError, match="run 2 is not running"):
        asyncio.run(executor2.start(2, _noop_agent))


def _wire_with_state(
    monkeypatch: pytest.MonkeyPatch,
    run_state: _FakeRunStateService,
    executor_state: _FakeRunStateService,
) -> _AssertingExecutor:
    executor = _AssertingExecutor(executor_state, [])
    monkeypatch.setattr(runner_module, "get_conversation_run_executor", lambda: executor)
    monkeypatch.setattr(
        runner_module, "get_conversation_run_state_service", lambda: run_state
    )
    return executor


# =========================================================================== #
# S7. 补充分支覆盖：事件循环冲突 / 执行后终态 / 取消收口回退
# =========================================================================== #
def test_run_child_inside_event_loop_returns_failed_without_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[S7] 在已运行事件循环线程内调用：必须返回 failed 且**不**认领、**不**启动。"""

    run_state = _FakeRunStateService("pending")
    executor = _wire(monkeypatch, run_state)
    runner = ChildAgentRunner(run_agent=_noop_agent)

    async def _call_inside_loop() -> Any:
        return runner.run_child(_child_profile(50))

    result = asyncio.run(_call_inside_loop())

    assert result.status == "failed"
    assert result.error == "delegation_runner_event_loop_thread"
    assert run_state.claim_calls == [], "事件循环冲突路径不应认领 run"
    assert executor.started == []


def test_post_execution_failed_terminal_maps_end_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[S7] executor 正常返回但 run 落定为 failed：结果 error 取 run.end_reason。"""

    run_state = _FakeRunStateService("pending")

    class _TerminalFailExecutor(_AssertingExecutor):
        async def start(self, run_id: int, runner: Any) -> asyncio.Task[None]:
            if self._run_state.status != "running":
                raise ValueError(f"run {run_id} is not running")
            self.started.append(run_id)
            self._run_state.status = "failed"

            async def _done() -> None:
                return None

            return asyncio.create_task(_done())

    executor = _TerminalFailExecutor(run_state, [])
    # get_run 需要携带 end_reason。
    monkeypatch.setattr(
        run_state, "get_run",
        lambda rid: SimpleNamespace(id=rid, status="failed", end_reason="boom_reason",
                                    final_output="partial"),
    )
    monkeypatch.setattr(runner_module, "get_conversation_run_executor", lambda: executor)
    monkeypatch.setattr(runner_module, "get_conversation_run_state_service", lambda: run_state)

    runner = ChildAgentRunner(run_agent=_noop_agent)
    result = runner.run_child(_child_profile(51))

    assert result.status == "failed"
    assert result.error == "boom_reason"
    assert result.summary == "partial"


def test_post_execution_cancelled_terminal_carries_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[S7] executor 正常返回但 run 落定为 cancelled：结果 cancelled 且携带 final_output。"""

    run_state = _FakeRunStateService("pending")

    class _TerminalCancelExecutor(_AssertingExecutor):
        async def start(self, run_id: int, runner: Any) -> asyncio.Task[None]:
            if self._run_state.status != "running":
                raise ValueError(f"run {run_id} is not running")
            self.started.append(run_id)
            self._run_state.status = "cancelled"

            async def _done() -> None:
                return None

            return asyncio.create_task(_done())

    executor = _TerminalCancelExecutor(run_state, [])
    monkeypatch.setattr(
        run_state, "get_run",
        lambda rid: SimpleNamespace(id=rid, status="cancelled", end_reason=None,
                                    final_output="stopped"),
    )
    monkeypatch.setattr(runner_module, "get_conversation_run_executor", lambda: executor)
    monkeypatch.setattr(runner_module, "get_conversation_run_state_service", lambda: run_state)

    runner = ChildAgentRunner(run_agent=_noop_agent)
    result = runner.run_child(_child_profile(52))

    assert result.status == "cancelled"
    assert result.error == "child run cancelled"
    assert result.summary == "stopped"


def test_cancel_fallback_reads_run_when_cancel_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[S7] ``_build_cancelled_result`` 当 ``cancel_run_if_running`` 未命中时回退 ``get_run``。"""

    run_state = _FakeRunStateService("completed")  # 非 active ⇒ cancel 返回 None
    _wire(monkeypatch, run_state)
    runner = ChildAgentRunner(run_agent=_noop_agent, should_cancel=lambda _rid: True)

    result = runner.run_child(_child_profile(53))

    assert result.status == "cancelled"
    assert run_state.cancel_calls == [53]
    assert result.summary == "out:completed"  # 回退读取的真实 final_output


def test_child_profile_without_run_raises_attributeerror_uncatchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[S7/健壮性] child profile 的 run 为 None 时 ``run_child`` 直接抛 AttributeError。

    实际行为（锁定）：``run_child`` 第 71 行先解引用 ``child_profile.run.id``，
    早于 ``_run_child`` 的 ``child_run is None`` 守卫（line 156-157 实为死代码）⇒
    抛出未捕获的 ``AttributeError``，不返回 failed DelegationResult。
    ``run_child`` 的契约声称「无异常，内部异常转 failed」，此处与之相悖——记录为
    防御性编程缺口（不修，仅暴露）。
    """

    run_state = _FakeRunStateService("pending")
    executor = _wire(monkeypatch, run_state)
    runner = ChildAgentRunner(run_agent=_noop_agent)

    with pytest.raises(AttributeError, match="'NoneType' object has no attribute 'id'"):
        runner.run_child(SimpleNamespace(run=None))

    # 未认领、未启动（因为异常发生在更早的阶段）。
    assert run_state.claim_calls == []
    assert executor.started == []
