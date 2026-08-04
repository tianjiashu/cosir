"""CodeGraph Kernel 启动接线单元测试。

覆盖 ``app.api.app._start_codegraph_kernel`` 的降级语义：
- 启动成功：supervisor 设进程级单例并返回；
- 启动失败（node 缺失 / dist 未构建 / 握手失败）：异常被捕获、仍设单例、不向上抛；
- 失败后 supervisor.state 为 failed（供 get_client 降级）。
"""

from __future__ import annotations

import asyncio

from app.api.app import _start_codegraph_kernel


class _FakeSupervisor:
    """可控 start 行为与 state 的假 supervisor。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.state = "failed" if fail else "ready"
        self.start_called = False

    def start(self) -> None:
        self.start_called = True
        if self.fail:
            raise RuntimeError("dist not built")


def test_startup_success_sets_singleton(monkeypatch):
    """启动成功：调用 start、设进程级单例、返回 supervisor。"""
    recorded: list[object] = []
    fake = _FakeSupervisor(fail=False)

    monkeypatch.setattr("app.codegraph.CodeGraphKernelSupervisor", lambda: fake)
    monkeypatch.setattr("app.codegraph.set_kernel_supervisor", lambda s: recorded.append(s))

    result = asyncio.run(_start_codegraph_kernel())

    assert fake.start_called is True
    assert result is fake
    assert recorded == [fake]


def test_startup_failure_degrades_not_raise(monkeypatch):
    """启动失败：异常被捕获、仍设单例、不向上抛。"""
    recorded: list[object] = []
    fake = _FakeSupervisor(fail=True)

    monkeypatch.setattr("app.codegraph.CodeGraphKernelSupervisor", lambda: fake)
    monkeypatch.setattr("app.codegraph.set_kernel_supervisor", lambda s: recorded.append(s))

    # 不应抛异常
    result = asyncio.run(_start_codegraph_kernel())

    assert fake.start_called is True
    assert result is fake
    assert recorded == [fake]
    # 失败后 state 为 failed，供 get_client 抛 unavailable → service 层降级
    assert result.state == "failed"


def test_startup_always_registers_singleton(monkeypatch):
    """无论成败都 set_kernel_supervisor，保证 get_kernel_supervisor 不抛 RuntimeError。"""
    import app.codegraph

    # 用真实 set_kernel_supervisor + 假 supervisor，验证单例被设置
    fake = _FakeSupervisor(fail=False)
    monkeypatch.setattr("app.codegraph.CodeGraphKernelSupervisor", lambda: fake)

    asyncio.run(_start_codegraph_kernel())

    supervisor = app.codegraph.get_kernel_supervisor()
    assert supervisor is fake
    # 清理，避免污染其它测试
    app.codegraph._SUPERVISOR = None
