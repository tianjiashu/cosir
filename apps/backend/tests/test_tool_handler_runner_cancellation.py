"""ToolHandlerRunner 取消链路 + process/thread 执行边界的独立单元测试。

覆盖：
1. ``_is_cancelled`` 的**实时性**（防「布尔快照」回退 bug）；
2. process 模式正常执行（回归：不再抛 ``TypeError: 'bool' object is not callable``）；
3. process 模式执行途中取消：强杀子进程、耗时远小于 handler 睡眠、标记文件不产生；
4. process 模式启动前已取消：不派生进程、耗时极短；
5. thread 模式执行前取消 / 正常执行；
6. 边界：超时强杀仍有效。

Windows 上 multiprocessing 为 spawn：自定义 handler 必须是**模块级函数**
（本例用 ``functools.partial`` 绑定参数），且测试模块导入时不得有任何副作用
（除模块级函数 / 常量 / pydantic 模型定义外不执行逻辑）。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel

from app.core.tool.schemas import ToolDefinition, ToolExecutionContext
from app.core.tool.execute import tool_handler_runner as runner_module, tool_handler_runner as mod
from app.core.tool.execute.tool_handler_runner import ToolHandlerRunner
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry


@pytest.fixture(autouse=True)
def _neutralize_setsid_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽子进程入口的 ``os.setsid()``，让进程内 fake 子进程不污染宿主会话。

    真实子进程里 ``os.setsid()`` 只是自立为进程组组长（供 ``_force_kill`` 的
    ``os.killpg`` 清理孙进程）。但本模块的 ``_FakeProcess`` 在同一进程的**线程**内
    执行 ``_execute_handler``，此时 ``os.setsid()`` 实际改的是 pytest 进程自身的会话：
    首次调用会把 pytest 变成会话首进程，之后所有调用都因 EPERM 抛 ``PermissionError``，
    导致排在后面的 fake 子进程用例出现「顺序相关」的偶发失败。这里把该副作用收敛为
    无操作，既保留被覆盖的父进程等待/归一化分支，又让用例结果与执行顺序解耦。
    """

    monkeypatch.setattr(os, "setsid", lambda: None, raising=False)


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


def _assert_runtime_cancelled(observation) -> None:
    """断言执行器取消路径使用当前的运行期取消诊断文案。"""

    assert observation.status == "cancelled"
    assert observation.content.startswith("cancelled: the user stopped this tool call mid-execution")


def _handler_emits_output(execution_context=None, output_sink=None, **_kwargs):
    """在真实隔离子进程中发送数个实时片段并返回结果。"""
    if output_sink is not None:
        output_sink("first chunk")
        output_sink("last chunk")
    return "done"


def _handler_sleeps_then_writes(
    marker_path: str, sleep_seconds: float, execution_context=None, **_kwargs
):
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
        group="probe",
        description="test tool",
        handler=handler,
        args_model=_NoArgs,
        timeout_seconds=timeout_seconds,
        execution_mode=execution_mode,
    )


_TASK_ID = 1


def _make_context(run_id: int, *, tool_call_id: str = "") -> ToolExecutionContext:
    """构造绑定了指定 run_id 与调用身份的执行上下文；Task 级取消信号按 ``_TASK_ID`` 记录。

    runner 只从 ``execution_context.tool_call_id`` 读取调用身份，不再接收单独的参数。
    """

    return ToolExecutionContext(
        step_id="step-1",
        task_id=_TASK_ID,
        workspace_id=1,
        workspace_root=Path("."),
        run_id=run_id,
        tool_call_id=tool_call_id,
    )


# ---------------------------------------------------------------------------
# 用例 1: _is_cancelled 实时性
# ---------------------------------------------------------------------------


def test_is_cancelled_is_live_not_snapshot() -> None:
    """实时性：同一个绑定了上下文的检查在 mark 前后返回值翻转，clear 后又变回 False。

    潜在缺陷：把「布尔快照」当作检查结果（执行开始时只读一次），导致工具启动
    之后的取消永远查不到，取消分支形同死代码。
    """

    run_id = 900001
    cancellation_registry.consume(_TASK_ID)
    try:
        check = partial(ToolHandlerRunner._is_cancelled, _make_context(run_id))
        # mark 之前：False
        assert check() is False
        # mark 之后：同一回调返回 True（实时读取注册表）
        cancellation_registry.request_cancel(_TASK_ID)
        assert check() is True
        # clear 之后：又变回 False
        cancellation_registry.consume(_TASK_ID)
        assert check() is False
    finally:
        cancellation_registry.consume(_TASK_ID)


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
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0, tool_call_id="call-1"))

    assert observation.status == "success"
    assert observation.content == "hello-from-process"
    assert observation.tool_call_id == "call-1"


def test_process_mode_flushes_terminal_output_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 multiprocessing 队列在工具返回前 flush 通用输出通道。"""
    received: list[str] = []
    finished: list[bool] = []

    class Channel:
        def emit(self, text: str) -> None:
            received.append(text)

        def finish(self) -> None:
            finished.append(True)

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(
        runner_module, "create_process_output_channel", lambda _tool_name, _context: Channel()
    )
    tool = _make_tool(
        _handler_emits_output,
        name="proc_output",
        execution_mode="process",
        timeout_seconds=10.0,
    )

    context = replace(
        _make_context(0),
        tool_call_id="call-output",
    )
    try:
        observation = ToolHandlerRunner().execute(
            tool,
            {},
            context,
        )
    finally:
        loop.close()

    assert observation.status == "success"
    assert observation.content == "done"
    assert received == ["first chunk", "last chunk"]
    assert finished == [True]


# ---------------------------------------------------------------------------
# 用例 3: process 模式执行途中取消
# ---------------------------------------------------------------------------


def test_process_mode_cancelled_mid_execution_kills_child(tmp_path: Path) -> None:
    """执行途中取消 -> status="cancelled"、reason 为统一常量、耗时远小于睡眠、标记文件不存在。

    潜在缺陷：取消未被实时检出（快照 bug）导致 handler 跑完；或未强杀子进程，
    标记文件仍被写出；或 reason 未使用统一常量。
    """

    run_id = 900002
    cancellation_registry.consume(_TASK_ID)
    marker = tmp_path / "marker.txt"
    sleep_seconds = 5.0

    tool = _make_tool(
        partial(_handler_sleeps_then_writes, str(marker), sleep_seconds),
        name="proc_cancel",
        execution_mode="process",
        timeout_seconds=30.0,
    )

    timer = threading.Timer(0.3, cancellation_registry.request_cancel, args=(_TASK_ID,))
    timer.start()
    started = time.monotonic()
    try:
        observation = ToolHandlerRunner().execute(
            tool, {}, _make_context(run_id, tool_call_id="call-cancel")
        )
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()
        cancellation_registry.consume(_TASK_ID)

    _assert_runtime_cancelled(observation)
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
    cancellation_registry.consume(_TASK_ID)
    cancellation_registry.request_cancel(_TASK_ID)
    try:
        tool = _make_tool(
            _handler_returns_string,
            name="proc_pre_cancel",
            execution_mode="process",
            timeout_seconds=10.0,
        )
        started = time.monotonic()
        observation = ToolHandlerRunner().execute(
            tool, {}, _make_context(run_id, tool_call_id="call-pre")
        )
        elapsed = time.monotonic() - started
    finally:
        cancellation_registry.consume(_TASK_ID)

    _assert_runtime_cancelled(observation)
    # 不派生进程：远小于一次 spawn 的启动成本。
    assert elapsed < 0.5, f"启动前取消耗时 {elapsed:.3f}s 过长，疑似仍派生了子进程"


# ---------------------------------------------------------------------------
# 用例 5: thread 模式
# ---------------------------------------------------------------------------


def test_thread_mode_cancelled_before_execution() -> None:
    """thread 模式执行前已取消 -> status="cancelled"，handler 不执行。"""

    run_id = 900004
    cancellation_registry.consume(_TASK_ID)
    cancellation_registry.request_cancel(_TASK_ID)
    try:
        tool = _make_tool(_handler_returns_string, name="thread_pre", execution_mode="thread")
        observation = ToolHandlerRunner().execute(
            tool, {}, _make_context(run_id, tool_call_id="call-thread-pre")
        )
    finally:
        cancellation_registry.consume(_TASK_ID)

    _assert_runtime_cancelled(observation)


def test_thread_mode_normal_execution_returns_success() -> None:
    """thread 模式正常执行 -> status="success"。"""

    tool = _make_tool(_handler_returns_string, name="thread_norm", execution_mode="thread")
    observation = ToolHandlerRunner().execute(
        tool, {}, _make_context(0, tool_call_id="call-thread")
    )

    assert observation.status == "success"
    assert observation.content == "hello-from-process"


# ---------------------------------------------------------------------------
# 用例 6: 边界
# ---------------------------------------------------------------------------


def test_process_mode_timeout_still_kills_child() -> None:
    """超时路径不受取消改造影响：handler 睡 10s + timeout_seconds=0.5 -> error 且 <3s。

    潜在缺陷：新增取消轮询破坏了原超时判定，导致超时不再强杀。
    """

    run_id = 900005
    cancellation_registry.consume(_TASK_ID)
    tool = _make_tool(
        partial(_handler_sleeps, 10.0),
        name="proc_timeout",
        execution_mode="process",
        timeout_seconds=0.5,
    )

    started = time.monotonic()
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(run_id))
    elapsed = time.monotonic() - started
    cancellation_registry.consume(_TASK_ID)

    assert observation.status == "error"
    assert elapsed < 3.0, f"超时强杀耗时 {elapsed:.2f}s 过长，疑似未强杀"


# ---------------------------------------------------------------------------
# 附加：不派生进程的内部路径（提高目标模块覆盖，且在 --cov 下可稳定运行）
# ---------------------------------------------------------------------------


def test_normalize_result_wraps_scalar_and_structured_payloads() -> None:
    """_normalize_result：标量走 str()，dict/list 走 json.dumps。

    ToolObservation 透传并补 tool_call_id。

    潜在缺陷：结构化数据被 str() 化为 Python repr（含单引号），模型无法解析。
    """

    from dataclasses import replace

    from app.core.tool.schemas import ToolObservation

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


class _FakeProcess:
    """在后台线程内运行 target 的假子进程：模拟 start/is_alive/terminate/kill/join/pid。

    target 完成后线程结束，``is_alive`` 返回 False；用于覆盖父进程侧等待/归一化分支。
    """

    instances: ClassVar[list[_FakeProcess]] = []

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

    monkeypatch.setattr(mod.multiprocessing, "Process", _FakeProcess)
    # result_queue 用 multiprocessing.Queue 的进程内替代（仅需 get/get_nowait/cancel_join_thread）。
    monkeypatch.setattr(mod.multiprocessing, "Queue", _make_fake_queue_factory())

    tool = _make_tool(
        _handler_returns_string, name="fake_success", execution_mode="process", timeout_seconds=5.0
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0, tool_call_id="fake-1"))

    assert observation.status == "success"
    assert observation.content == "hello-from-process"
    assert observation.tool_call_id == "fake-1"


def test_execute_in_process_with_fake_process_handler_error(monkeypatch) -> None:
    """fake 子进程 handler 抛错 -> error payload -> 归一化为 handler_failed。"""

    from app.core.tool.execute import tool_handler_runner as mod

    monkeypatch.setattr(mod.multiprocessing, "Process", _FakeProcess)
    monkeypatch.setattr(mod.multiprocessing, "Queue", _make_fake_queue_factory())

    tool = _make_tool(
        _handler_raises, name="fake_err", execution_mode="process", timeout_seconds=5.0
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))

    assert observation.status == "error"
    assert "hint: do not retry this tool call." in observation.content


def test_execute_in_process_with_fake_process_exited_without_result(monkeypatch) -> None:
    """子进程「已退出且无结果」-> 返回 error（exited without a result 分支）。"""

    from app.core.tool.execute import tool_handler_runner as mod

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

    from app.core.tool.execute import tool_handler_runner as mod

    class _AliveForever:
        def is_alive(self) -> bool:
            return True

    q = _make_fake_queue_factory()(maxsize=1)
    with pytest.raises(TimeoutError):
        mod.ToolHandlerRunner._wait_for_result(
            _AliveForever(), q, timeout=0.2, is_cancelled=lambda: False
        )


def test_wait_for_result_raises_cancelled_when_is_cancelled_true() -> None:
    """_wait_for_result：is_cancelled 命中 -> 抛内部 _ToolExecutionCancelled。

    潜在缺陷：取消检查被放在阻塞 get 之后，导致取消无法及时中断等待。
    """

    from app.core.tool.execute import tool_handler_runner as mod

    class _AliveForever:
        def is_alive(self) -> bool:
            return True

    q = _make_fake_queue_factory()(maxsize=1)
    with pytest.raises(mod._ToolExecutionCancelled):
        mod.ToolHandlerRunner._wait_for_result(
            _AliveForever(), q, timeout=5.0, is_cancelled=lambda: True
        )


def _make_fake_queue_factory():
    """返回具备进程队列接口的进程内替代工厂。"""

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

    from app.core.tool.execute import tool_handler_runner as mod

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

    from app.core.tool.execute import tool_handler_runner as mod

    proc = _AliveProcess()

    def _raise_cancel(*_args, **_kwargs):
        raise mod._ToolExecutionCancelled()

    _patch_runner_deps(monkeypatch, process=proc, wait_impl=_raise_cancel)
    tool = _make_tool(
        _handler_returns_string, name="cancel_path", execution_mode="process", timeout_seconds=5.0
    )
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))

    _assert_runtime_cancelled(observation)
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
    assert "hint: this error can be retried" in observation.content
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
    assert "hint: do not retry this tool call." in observation.content


def test_thread_handler_exception_returns_error() -> None:
    """thread 模式 handler 抛异常 -> status="error"（thread 异常归一化分支）。"""

    tool = _make_tool(_handler_raises, name="thread_err", execution_mode="thread")
    observation = ToolHandlerRunner().execute(tool, {}, _make_context(0))

    assert observation.status == "error"
    assert "hint: do not retry this tool call." in observation.content


def test_thread_mode_keeps_result_when_cancelled_during_execution() -> None:
    """thread 模式：handler 已跑完就保留真实结果；执行期间到达的取消交给循环下一个检查点。

    改判为 cancelled 会让模型以为写盘等副作用没有发生而重复执行。
    """

    def _handler_marks_cancel(execution_context=None, **_kwargs):
        cancellation_registry.request_cancel(_TASK_ID)
        return "kept"

    tool = _make_tool(_handler_marks_cancel, name="thread_post", execution_mode="thread")
    try:
        observation = ToolHandlerRunner().execute(tool, {}, _make_context(900006))
    finally:
        cancellation_registry.consume(_TASK_ID)

    assert observation.status == "success"


def test_execute_in_process_uses_runtime_output_channel_factory(monkeypatch) -> None:
    """运行期 factory 返回通道后，process 路径建立输出队列并收尾通道。"""

    from app.core.tool.execute import tool_handler_runner as mod

    monkeypatch.setattr(mod.multiprocessing, "Process", _FakeProcess)
    monkeypatch.setattr(mod.multiprocessing, "Queue", _make_fake_queue_factory())

    seen: list[tuple[str, bool]] = []
    finished = threading.Event()

    class Channel:
        def emit(self, text: str) -> None:
            seen.append((text, False))

        def finish(self) -> None:
            finished.set()

    def _handler_emits(execution_context=None, output_sink=None, **_kwargs):
        # 子进程入口会把 output_sink 注入 handler（本 fake 在同线程运行）。
        if output_sink is not None:
            output_sink("chunk-1")
        return "done"

    tool = _make_tool(
        _handler_emits, name="sink_tool", execution_mode="process", timeout_seconds=5.0
    )
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(
        runner_module, "create_process_output_channel", lambda _tool_name, _context: Channel()
    )
    context = replace(
        _make_context(0),
        tool_call_id="call-output",
    )
    observation = ToolHandlerRunner().execute(
        tool,
        {},
        context,
    )
    loop.close()

    assert observation.status == "success"
    assert observation.content == "done"
    assert seen == [("chunk-1", False)]
    assert finished.is_set()
