"""ToolHandlerRunner 取消链路 + process/thread 执行边界的独立单元测试。

覆盖：
1. ``_build_cancel_check`` 的返回契约与**实时性**（防「布尔快照」回退 bug）；
2. process 模式正常执行（回归：不再抛 ``TypeError: 'bool' object is not callable``）；
3. process 模式执行途中取消：强杀子进程、耗时远小于 handler 睡眠、标记文件不产生；
4. process 模式启动前已取消：不派生进程、耗时极短；
5. thread 模式执行前取消 / 正常执行；
6. 边界：``execution_context=None`` 不抛 AttributeError；超时强杀仍有效。

Windows 上 multiprocessing 为 spawn：自定义 handler 必须是**模块级函数**
（本例用 ``functools.partial`` 绑定参数），且测试模块导入时不得有任何副作用
（除模块级函数 / 常量 / pydantic 模型定义外不执行逻辑）。
"""

from __future__ import annotations

import threading
import time
from functools import partial
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.schemas import ToolDefinition, ToolExecutionContext
from app.core.tools.tool_execute.tool_cancelled import CANCELLED_REASON
from app.core.tools.tool_execute.tool_handler_runner import ToolHandlerRunner


# ---------------------------------------------------------------------------
# 最小参数校验契约模型（args_model 在执行路径上不被消费，仅需为 pydantic BaseModel）
# ---------------------------------------------------------------------------


class _NoArgs(BaseModel):
    """空参数模型：测试工具均不需要入参。"""


# ---------------------------------------------------------------------------
# 模块级 handler 实现（spawn 子进程需可 pickle 的模块级可调用对象）
# ---------------------------------------------------------------------------


def _handler_returns_string(execution_context=None, **_kwargs):
    """process / thread 正常路径：返回普通字符串。"""

    return "hello-from-process"


def _handler_sleeps_then_writes(marker_path: str, sleep_seconds: float, execution_context=None, **_kwargs):
    """取消路径：先长睡，再写标记文件，用于证明子进程被强杀（未跑完）。"""

    time.sleep(sleep_seconds)
    Path(marker_path).write_text("finished", encoding="utf-8")
    return "should-not-finish"


def _handler_sleeps(sleep_seconds: float, execution_context=None, **_kwargs):
    """超时路径：纯睡不产出。"""

    time.sleep(sleep_seconds)
    return "should-not-finish"


# ---------------------------------------------------------------------------
# 工厂辅助
# ---------------------------------------------------------------------------


def _make_tool(
    handler,
    *,
    name: str = "test_tool",
    execution_mode: str = "thread",
    timeout_seconds: float = 10.0,
) -> ToolDefinition:
    """构造测试用 ToolDefinition。"""

    return ToolDefinition(
        name=name,
        description="test tool",
        permission="test",
        handler=handler,
        args_model=_NoArgs,
        timeout_seconds=timeout_seconds,
        execution_mode=execution_mode,
    )


def _make_context(run_id: int) -> ToolExecutionContext:
    """构造绑定了指定 run_id 的执行上下文。"""

    return ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=Path("."),
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# 用例 1: _build_cancel_check 契约与实时性
# ---------------------------------------------------------------------------


def test_build_cancel_check_none_context_returns_none() -> None:
    """execution_context=None -> 返回 None（视为不可取消）。

    潜在缺陷：未做 None 守卫，构造期访问 run_id 抛 AttributeError。
    """

    assert ToolHandlerRunner._build_cancel_check(None) is None


def test_build_cancel_check_zero_run_id_returns_none() -> None:
    """run_id=0（无 run 绑定）-> 返回 None。

    潜在缺陷：漏判 0 边界，返回了永远 False 的回调。
    """

    assert ToolHandlerRunner._build_cancel_check(_make_context(0)) is None


def test_build_cancel_check_negative_run_id_returns_none() -> None:
    """run_id<0 -> 返回 None（<=0 边界）。

    潜在缺陷：仅判 ==0 漏掉负值。
    """

    assert ToolHandlerRunner._build_cancel_check(_make_context(-5)) is None


def test_build_cancel_check_positive_run_id_returns_callable() -> None:
    """run_id>0 -> 返回可调用对象。"""

    check = ToolHandlerRunner._build_cancel_check(_make_context(123456))
    assert callable(check)


def test_build_cancel_check_is_live_not_snapshot() -> None:
    """实时性：mark 前后同一回调返回值翻转，clear 后又变回 False。

    潜在缺陷：把「布尔快照」当作回调返回（构造前只读一次 run 状态），导致工具启动
    之后的取消永远查不到，取消分支形同死代码。
    """

    run_id = 900001
    cancellation_registry.clear(run_id)
    try:
        check = ToolHandlerRunner._build_cancel_check(_make_context(run_id))
        assert check is not None
        # mark 之前：False
        assert check() is False
        # mark 之后：同一回调返回 True（实时读取注册表）
        cancellation_registry.mark_cancelled(run_id)
        assert check() is True
        # clear 之后：又变回 False
        cancellation_registry.clear(run_id)
        assert check() is False
    finally:
        cancellation_registry.clear(run_id)


# ---------------------------------------------------------------------------
# 用例 2: process 模式正常执行
# ---------------------------------------------------------------------------


def test_process_mode_normal_execution_returns_success() -> None:
    """process 模式正常执行 -> status="success"。

    潜在缺陷（回归）：取消检查被误当真值调用，抛
    ``TypeError: 'bool' object is not callable``。
    """

    tool = _make_tool(
        _handler_returns_string, name="proc_norm", execution_mode="process", timeout_seconds=10.0
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0), tool_call_id="call-1")

    assert observation.status == "success"
    assert observation.content == "hello-from-process"
    assert observation.tool_call_id == "call-1"


# ---------------------------------------------------------------------------
# 用例 3: process 模式执行途中取消
# ---------------------------------------------------------------------------


def test_process_mode_cancelled_mid_execution_kills_child(tmp_path: Path) -> None:
    """执行途中取消 -> status="cancelled"、reason 为统一常量、耗时远小于睡眠、标记文件不存在。

    潜在缺陷：取消未被实时检出（快照 bug）导致 handler 跑完；或未强杀子进程，
    标记文件仍被写出；或 reason 未使用统一常量。
    """

    run_id = 900002
    cancellation_registry.clear(run_id)
    marker = tmp_path / "marker.txt"
    sleep_seconds = 5.0

    tool = _make_tool(
        partial(_handler_sleeps_then_writes, str(marker), sleep_seconds),
        name="proc_cancel",
        execution_mode="process",
        timeout_seconds=30.0,
    )

    timer = threading.Timer(0.3, cancellation_registry.mark_cancelled, args=(run_id,))
    timer.start()
    started = time.monotonic()
    try:
        observation = ToolHandlerRunner().execute(
            tool, {}, _make_context(run_id), tool_call_id="call-cancel"
        )
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()
        cancellation_registry.clear(run_id)

    assert observation.status == "cancelled"
    assert observation.reason == CANCELLED_REASON
    # 耗时必须远小于 handler 自身睡眠时间（证明是被强杀，而非跑完）。
    assert elapsed < sleep_seconds / 2, f"耗时 {elapsed:.2f}s 未体现强杀（睡眠 {sleep_seconds}s）"
    # 充分等待后标记文件仍不存在：证明子进程未跑完写文件。
    time.sleep(1.0)
    assert not marker.exists(), "标记文件存在 -> 子进程未被强杀，handler 跑完了"


# ---------------------------------------------------------------------------
# 用例 4: process 模式启动前已取消
# ---------------------------------------------------------------------------


def test_process_mode_cancelled_before_start_does_not_spawn() -> None:
    """启动前已取消 -> status="cancelled" 且耗时极短（不派生进程）。

    潜在缺陷：启动前取消检查缺失，仍 spawn 子进程后才取消（白付 spawn 代价）。
    """

    run_id = 900003
    cancellation_registry.clear(run_id)
    cancellation_registry.mark_cancelled(run_id)
    try:
        tool = _make_tool(
            _handler_returns_string,
            name="proc_pre_cancel",
            execution_mode="process",
            timeout_seconds=10.0,
        )
        started = time.monotonic()
        observation = ToolHandlerRunner().execute(
            tool, {}, _make_context(run_id), tool_call_id="call-pre"
        )
        elapsed = time.monotonic() - started
    finally:
        cancellation_registry.clear(run_id)

    assert observation.status == "cancelled"
    assert observation.reason == CANCELLED_REASON
    # 不派生进程：远小于一次 spawn 的启动成本。
    assert elapsed < 0.5, f"启动前取消耗时 {elapsed:.3f}s 过长，疑似仍派生了子进程"


# ---------------------------------------------------------------------------
# 用例 5: thread 模式
# ---------------------------------------------------------------------------


def test_thread_mode_cancelled_before_execution() -> None:
    """thread 模式执行前已取消 -> status="cancelled"，handler 不执行。"""

    run_id = 900004
    cancellation_registry.clear(run_id)
    cancellation_registry.mark_cancelled(run_id)
    try:
        tool = _make_tool(_handler_returns_string, name="thread_pre", execution_mode="thread")
        observation = ToolHandlerRunner().execute(
            tool, {}, _make_context(run_id), tool_call_id="call-thread-pre"
        )
    finally:
        cancellation_registry.clear(run_id)

    assert observation.status == "cancelled"
    assert observation.reason == CANCELLED_REASON


def test_thread_mode_normal_execution_returns_success() -> None:
    """thread 模式正常执行 -> status="success"。"""

    tool = _make_tool(_handler_returns_string, name="thread_norm", execution_mode="thread")
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0), tool_call_id="call-thread")

    assert observation.status == "success"
    assert observation.content == "hello-from-process"


# ---------------------------------------------------------------------------
# 用例 6: 边界
# ---------------------------------------------------------------------------


def test_execute_with_none_context_does_not_raise_attribute_error() -> None:
    """execution_context=None 调 execute() 不应抛 AttributeError。

    潜在缺陷：_build_cancel_check / 分支方法未做 None 守卫。
    """

    tool = _make_tool(_handler_returns_string, name="none_ctx", execution_mode="thread")
    observation = ToolHandlerRunner().execute(tool, {}, None, tool_call_id="call-nonectx")

    assert observation.status == "success"
    assert observation.content == "hello-from-process"


def test_process_mode_timeout_still_kills_child() -> None:
    """超时路径不受取消改造影响：handler 睡 10s + timeout_seconds=0.5 -> error 且 <3s。

    潜在缺陷：新增取消轮询破坏了原超时判定，导致超时不再强杀。
    """

    run_id = 900005
    cancellation_registry.clear(run_id)
    tool = _make_tool(
        partial(_handler_sleeps, 10.0),
        name="proc_timeout",
        execution_mode="process",
        timeout_seconds=0.5,
    )

    started = time.monotonic()
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(run_id))
    elapsed = time.monotonic() - started
    cancellation_registry.clear(run_id)

    assert observation.status == "error"
    assert elapsed < 3.0, f"超时强杀耗时 {elapsed:.2f}s 过长，疑似未强杀"


# ---------------------------------------------------------------------------
# 附加：不派生进程的内部路径（提高目标模块覆盖，且在 --cov 下可稳定运行）
# ---------------------------------------------------------------------------


def test_process_mode_missing_timeout_returns_error_without_spawn() -> None:
    """timeout_seconds=None -> 直接返回 error，不派生进程（防御性归一化分支）。

    潜在缺陷：未拦截 None 超时导致无限等待或抛 ValueError。
    """

    tool = ToolDefinition(
        name="no_timeout",
        description="d",
        permission="test",
        handler=_handler_returns_string,
        args_model=_NoArgs,
        timeout_seconds=None,
        execution_mode="process",
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))

    assert observation.status == "error"
    assert observation.retryable is False


def test_process_mode_non_positive_timeout_returns_error() -> None:
    """timeout_seconds<=0 -> 返回 error（<=0 边界）。"""

    tool = _make_tool(
        _handler_returns_string, name="zero_timeout", execution_mode="process", timeout_seconds=0.0
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))
    assert observation.status == "error"


def test_normalize_result_wraps_scalar_and_structured_payloads() -> None:
    """_normalize_result：标量走 str()，dict/list 走 json.dumps，ToolObservation 透传并补 tool_call_id。

    潜在缺陷：结构化数据被 str() 化为 Python repr（含单引号），模型无法解析。
    """

    from dataclasses import replace

    from app.core.tools.schemas import ToolObservation

    tool = _make_tool(_handler_returns_string, name="norm")
    runner = ToolHandlerRunner()

    scalar = runner._normalize_result(tool, 42, "cid")
    assert scalar.status == "success"
    assert scalar.content == "42"

    structured = runner._normalize_result(tool, {"a": [1, 2]}, "cid2")
    assert structured.content == '{"a": [1, 2]}'

    existing = ToolObservation(tool_name="norm", status="success", content="x", tool_call_id="")
    passed = runner._normalize_result(tool, existing, "cid3")
    assert passed.tool_call_id == "cid3"
    assert replace(existing, tool_call_id="cid3") == passed


def test_drain_output_queue_none_is_noop() -> None:
    """_drain_output_queue：队列或 sink 为 None 时直接返回，不抛异常。"""

    ToolHandlerRunner._drain_output_queue(None, None)
    ToolHandlerRunner._drain_output_queue(None, lambda *_: None)


def test_drain_output_queue_sink_failure_is_swallowed() -> None:
    """sink 抛异常不影响结果获取：drain 静默结束（旁路通道不得反压执行）。"""

    import queue as _queue

    q = _queue.Queue()
    q.put(("a", False))
    q.put(("b", True))
    seen: list[tuple[str, bool]] = []

    def _bad_sink(text, truncated):
        seen.append((text, truncated))
        raise RuntimeError("sink down")

    # 不应抛出。
    ToolHandlerRunner._drain_output_queue(q, _bad_sink)
    assert seen == [("a", False)]  # 首个片段触发异常后停止推送


# ---------------------------------------------------------------------------
# 附加：用「进程内 fake」覆盖 process 分支（start() 之后的归一化 / 等待逻辑），
# 保证在无法真实 spawn 的环境（--cov 插桩）下也能验证这些分支。
# ---------------------------------------------------------------------------


class _FakeProcess:
    """在后台线程内运行 target 的假子进程：模拟 start/is_alive/terminate/kill/join/pid。

    target 完成后线程结束，``is_alive`` 返回 False；用于覆盖父进程侧等待/归一化分支。
    """

    instances: list["_FakeProcess"] = []

    def __init__(self, *, target, args, daemon=True):
        self._target = target
        self._args = args
        self._daemon = daemon
        self.pid = 1000 + len(self.instances)
        self._thread: threading.Thread | None = None
        self.terminate_called = False
        self.kill_called = False
        _FakeProcess.instances.append(self)

    def start(self) -> None:
        def _run():
            self._target(*self._args)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True

    def join(self, timeout=None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


def test_execute_in_process_with_fake_process_success(monkeypatch) -> None:
    """用 fake 子进程覆盖 start() 之后的成功路径：result_queue 回写 success -> 归一化成功。

    潜在缺陷：父进程拿到 ("success", payload) 后未正确归一化（如误判 error）。
    """

    import multiprocessing

    from app.core.tools.tool_execute import tool_handler_runner as mod

    monkeypatch.setattr(mod.multiprocessing, "Process", _FakeProcess)
    # result_queue 用 multiprocessing.Queue 的进程内替代（仅需 get/get_nowait/cancel_join_thread）。
    monkeypatch.setattr(mod.multiprocessing, "Queue", _make_fake_queue_factory())

    tool = _make_tool(
        _handler_returns_string, name="fake_success", execution_mode="process", timeout_seconds=5.0
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0), tool_call_id="fake-1")

    assert observation.status == "success"
    assert observation.content == "hello-from-process"
    assert observation.tool_call_id == "fake-1"


def test_execute_in_process_with_fake_process_handler_error(monkeypatch) -> None:
    """fake 子进程内 handler 抛异常 -> ("error", payload) -> 归一化为 error（handler_failed 分支）。"""

    from app.core.tools.tool_execute import tool_handler_runner as mod

    monkeypatch.setattr(mod.multiprocessing, "Process", _FakeProcess)
    monkeypatch.setattr(mod.multiprocessing, "Queue", _make_fake_queue_factory())

    tool = _make_tool(
        _handler_raises, name="fake_err", execution_mode="process", timeout_seconds=5.0
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))

    assert observation.status == "error"
    assert observation.retryable is False


def test_execute_in_process_with_fake_process_exited_without_result(monkeypatch) -> None:
    """子进程「已退出且无结果」-> 返回 error（exited without a result 分支）。"""

    from app.core.tools.tool_execute import tool_handler_runner as mod

    class _DeadProcess(_FakeProcess):
        def start(self) -> None:
            # 不运行 target：模拟进程秒退、队列无结果。
            self._thread = threading.Thread(target=lambda: None, daemon=True)
            self._thread.start()
            self._thread.join()

    monkeypatch.setattr(mod.multiprocessing, "Process", _DeadProcess)
    monkeypatch.setattr(mod.multiprocessing, "Queue", _make_fake_queue_factory())

    tool = _make_tool(
        _handler_returns_string, name="fake_dead", execution_mode="process", timeout_seconds=0.5
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))

    assert observation.status == "error"


def test_wait_for_result_raises_timeout_when_process_alive(monkeypatch) -> None:
    """_wait_for_result：进程仍存活且超时 -> 抛 TimeoutError（超时终态）。

    潜在缺陷：超时未触发或返回了伪结果。
    """

    from app.core.tools.tool_execute import tool_handler_runner as mod

    class _AliveForever:
        def is_alive(self) -> bool:
            return True

    q = _make_fake_queue_factory()(maxsize=1)
    with pytest.raises(TimeoutError):
        mod.ToolHandlerRunner._wait_for_result(_AliveForever(), q, timeout=0.2)


def test_wait_for_result_raises_cancelled_when_should_cancel_true() -> None:
    """_wait_for_result：should_cancel 命中 -> 抛内部 _ToolExecutionCancelled。

    潜在缺陷：取消检查被放在阻塞 get 之后，导致取消无法及时中断等待。
    """

    from app.core.tools.tool_execute import tool_handler_runner as mod

    class _AliveForever:
        def is_alive(self) -> bool:
            return True

    q = _make_fake_queue_factory()(maxsize=1)
    with pytest.raises(mod._ToolExecutionCancelled):
        mod.ToolHandlerRunner._wait_for_result(
            _AliveForever(), q, timeout=5.0, should_cancel=lambda: True
        )


def _make_fake_queue_factory():
    """返回一个 multiprocessing.Queue 的进程内替代工厂（支持 maxsize / get / get_nowait / cancel_join_thread）。"""

    import queue as _queue

    def _factory(*args, **kwargs):
        q = _queue.Queue()

        def cancel_join_thread():
            return None

        def close():
            return None

        def join_thread():
            return None

        q.cancel_join_thread = cancel_join_thread  # type: ignore[attr-defined]
        q.close = close  # type: ignore[attr-defined]
        q.join_thread = join_thread  # type: ignore[attr-defined]
        return q

    return _factory


def _handler_raises(execution_context=None, **_kwargs):
    """模块级 handler：抛异常（process 子进程 error 分支）。"""

    raise RuntimeError("boom-in-handler")


# ---------------------------------------------------------------------------
# 附加：process 分支的异常终态（cancel / timeout / OSError）——用 fake 进程 + 补丁
# _wait_for_result 直接命中，避免真实 spawn。
# ---------------------------------------------------------------------------


class _AliveProcess:
    """始终存活的最小假进程（触发 finally 中的 _force_kill）。"""

    pid = 4321

    def __init__(self):
        self.terminate_called = False
        self.kill_called = False

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return True

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True

    def join(self, timeout=None) -> None:
        return None


def _patch_runner_deps(monkeypatch, *, process, wait_impl=None):
    """把 runner 模块内的 multiprocessing.Process/Queue 与 _wait_for_result 替换为测试替身。"""

    from app.core.tools.tool_execute import tool_handler_runner as mod

    class _P:
        def __new__(cls, *args, **kwargs):
            return process

    monkeypatch.setattr(mod.multiprocessing, "Process", _P)
    monkeypatch.setattr(mod.multiprocessing, "Queue", _make_fake_queue_factory())
    if wait_impl is not None:
        monkeypatch.setattr(mod.ToolHandlerRunner, "_wait_for_result", staticmethod(wait_impl))


def test_process_cancel_path_force_kills_and_returns_cancelled(monkeypatch) -> None:
    """等待期取消：_wait_for_result 抛 _ToolExecutionCancelled -> 返回 cancelled 并强杀进程。

    潜在缺陷：catch 分支写错导致取消异常逃逸 or 未强杀。
    """

    from app.core.tools.tool_execute import tool_handler_runner as mod

    proc = _AliveProcess()

    def _raise_cancel(*_args, **_kwargs):
        raise mod._ToolExecutionCancelled()

    _patch_runner_deps(monkeypatch, process=proc, wait_impl=_raise_cancel)
    tool = _make_tool(
        _handler_returns_string, name="cancel_path", execution_mode="process", timeout_seconds=5.0
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))

    assert observation.status == "cancelled"
    assert observation.reason == CANCELLED_REASON
    assert proc.terminate_called is True or proc.kill_called is True


def test_process_timeout_path_returns_error_and_retryable(monkeypatch) -> None:
    """等待期超时：_wait_for_result 抛 TimeoutError -> 返回 retryable=True 的 error 观察。

    潜在缺陷：超时被归一为非重试错误，或未强杀进程。
    """

    proc = _AliveProcess()

    def _raise_timeout(*_args, **_kwargs):
        raise TimeoutError()

    _patch_runner_deps(monkeypatch, process=proc, wait_impl=_raise_timeout)
    tool = _make_tool(
        _handler_returns_string, name="timeout_path", execution_mode="process", timeout_seconds=5.0
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))

    assert observation.status == "error"
    assert observation.retryable is True
    assert proc.terminate_called is True or proc.kill_called is True


def test_process_oserror_path_returns_error_not_raise(monkeypatch) -> None:
    """进程通信异常（OSError/EOFError）-> 归一为 error，不向上抛出（docstring 契约）。

    潜在缺陷：通信异常逃逸出 execute()，打断整个 turn。
    """

    proc = _AliveProcess()

    def _raise_oserror(*_args, **_kwargs):
        raise EOFError("pipe broken")

    _patch_runner_deps(monkeypatch, process=proc, wait_impl=_raise_oserror)
    tool = _make_tool(
        _handler_returns_string, name="oserr_path", execution_mode="process", timeout_seconds=5.0
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))

    assert observation.status == "error"
    assert observation.retryable is False


def test_force_kill_none_pid_is_noop() -> None:
    """_force_kill：pid 为 None 时直接返回（进程未启动），不抛异常。"""

    class _NoPid:
        pid = None

    ToolHandlerRunner()._force_kill(_NoPid())  # 不应抛出


def test_thread_handler_exception_returns_error() -> None:
    """thread 模式 handler 抛异常 -> status="error"（thread 异常归一化分支）。"""

    tool = _make_tool(_handler_raises, name="thread_err", execution_mode="thread")
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))

    assert observation.status == "error"
    assert observation.retryable is False


def test_thread_mode_post_cancel_discards_result() -> None:
    """thread 模式执行后边界取消：handler 已跑完但检测到取消 -> 丢弃结果转 cancelled。

    潜在缺陷：只在执行前检查，运行期取消后仍返回过期结果。
    """

    run_id = 900006
    cancellation_registry.clear(run_id)

    def _handler_marks_cancel(execution_context=None, **_kwargs):
        # 在 handler 执行期间标记取消（模拟运行中收到取消信号）。
        cancellation_registry.mark_cancelled(run_id)
        return "should-be-discarded"

    tool = _make_tool(_handler_marks_cancel, name="thread_post", execution_mode="thread")
    try:
        observation = ToolHandlerRunner().execute(tool, {}, _make_context(run_id))
    finally:
        cancellation_registry.clear(run_id)

    assert observation.status == "cancelled"
    assert observation.reason == CANCELLED_REASON


def test_execute_in_process_with_output_sink_uses_fake(monkeypatch) -> None:
    """带 output_sink 的 process 执行：建 output_queue 且 drain 回调 sink（实时通道分支）。"""

    from app.core.tools.tool_execute import tool_handler_runner as mod

    monkeypatch.setattr(mod.multiprocessing, "Process", _FakeProcess)
    monkeypatch.setattr(mod.multiprocessing, "Queue", _make_fake_queue_factory())

    seen: list[tuple[str, bool]] = []

    def _handler_emits(execution_context=None, output_sink=None, **_kwargs):
        # 子进程入口会把 output_sink 注入 handler（本 fake 在同线程运行）。
        if output_sink is not None:
            output_sink("chunk-1", False)
        return "done"

    tool = _make_tool(
        _handler_emits, name="sink_tool", execution_mode="process", timeout_seconds=5.0
    )
    observation = ToolHandlerRunner().execute(
        tool, {}, _make_context(0), output_sink=lambda text, truncated: seen.append((text, truncated))
    )

    assert observation.status == "success"
    assert observation.content == "done"

