"""CodeGraphKernelSupervisor 关停与退避重启的单元测试。

守护 P1-11：Kernel 崩溃进入退避重启（restart 线程 sleep 中）时调用 ``shutdown``，
sleep 结束后 restart 线程必须检查停机信号并放弃重启，**不得复活**已关停的 Kernel。
另守护正常退避重启路径仍工作（未停机时 sleep 后调用 start）。
"""

import time

import pytest

from app.codegraph.supervisor import CodeGraphKernelSupervisor, KernelState


def test_shutdown_aborts_pending_restart_and_does_not_resurrect() -> None:
    """shutdown 后 restart 线程 sleep 结束不复活 Kernel（P1-11 核心）。"""

    supervisor = CodeGraphKernelSupervisor()
    supervisor._set_state(KernelState.READY)
    supervisor._restart_attempts = 0

    # 模拟 health 崩溃：清理 + 启动退避重启线程（首个 backoff 为 0.5s）。
    supervisor._handle_crash()
    assert supervisor.state.value == KernelState.RESTARTING.value
    assert supervisor._restart_thread is not None
    assert supervisor._restart_thread.is_alive()

    # 退避 sleep 期间关停：置停机信号 + join restart 线程。
    supervisor.shutdown()
    assert supervisor.state.value == KernelState.STOPPED.value

    # sleep 结束后 restart 线程应因停机信号直接 return，不调用 start（不复活）。
    supervisor._join_threads()
    # 再等一小段，覆盖 restart 线程 sleep 结束后的判定窗口。
    time.sleep(0.8)
    assert supervisor.state.value == KernelState.STOPPED.value


def test_shutdown_twice_is_idempotent() -> None:
    """重复 shutdown 幂等：第二次直接返回，不抛错。"""

    supervisor = CodeGraphKernelSupervisor()
    supervisor._set_state(KernelState.READY)
    supervisor.shutdown()
    assert supervisor.state.value == KernelState.STOPPED.value
    supervisor.shutdown()
    assert supervisor.state.value == KernelState.STOPPED.value


def test_restart_still_happens_without_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """对照：未停机时 restart 线程 sleep 结束仍会调用 start 重启 Kernel。"""

    supervisor = CodeGraphKernelSupervisor()
    supervisor._set_state(KernelState.READY)
    supervisor._restart_attempts = 0

    calls: list[int] = []

    def fake_start() -> None:
        calls.append(1)

    monkeypatch.setattr(supervisor, "start", fake_start)

    supervisor._handle_crash()
    assert supervisor.state.value == KernelState.RESTARTING.value
    supervisor._join_threads()
    # join 最多等 2s，restart 线程 sleep 0.5s 后无停机信号应调用 start。
    assert calls, "未停机时退避重启应调用 start"
