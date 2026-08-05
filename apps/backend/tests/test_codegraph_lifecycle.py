"""CodeGraph 索引生命周期编排单元测试（对齐方案一第七节验收标准）。

覆盖：
- CodeGraphLifecycleService.ensure_ready 编排各分支：
  unindexed → init、ready → sync、indexing/failed → 降级；
- Kernel 不可用（supervisor 抛 KERNEL_UNAVAILABLE）→ 降级且不抛异常；
- InflightRegistry 的 singleflight 去重（同 key 并发只执行一次）。

不覆盖：真实 Kernel 进程（归端到端冒烟，避免测试依赖 node 运行时）。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.codegraph import (
    CodeGraphKernelClient,
    CodeGraphKernelUnavailableError,
    IndexInitResult,
    IndexStatusResult,
    IndexSyncResult,
)
from app.models.workspace_readiness import WorkspaceReadiness
from app.service.codegraph_lifecycle_service import CodeGraphLifecycleService
from app.utils.inflight_registry import InflightRegistry


class _FakeClient:
    """可编程的 Kernel client 桩：按配置返回 status 与记录调用。"""

    def __init__(self, status: IndexStatusResult) -> None:
        self._status = status
        self.init_calls: list[str] = []
        self.sync_calls: list[str] = []

    def index_status(self, workspace_path: str, timeout: float | None = None) -> IndexStatusResult:
        return self._status

    def index_init(self, workspace_path: str, timeout: float | None = None) -> IndexInitResult:
        self.init_calls.append(workspace_path)
        # 模拟真实 init 的耗时，让 singleflight 的并发窗口成立（否则瞬时完成会错过去重）。
        time.sleep(0.05)
        return IndexInitResult(state="ready", files_indexed=42, duration_ms=100)

    def index_sync(self, workspace_path: str, timeout: float | None = None) -> IndexSyncResult:
        self.sync_calls.append(workspace_path)
        return IndexSyncResult(
            state="ready",
            files_added=1,
            files_modified=2,
            files_removed=0,
            duration_ms=50,
        )


class _UnavailableClient:
    """index_status 恒抛 KERNEL_UNAVAILABLE 的桩。"""

    def index_status(self, workspace_path: str, timeout: float | None = None):
        raise CodeGraphKernelUnavailableError("kernel not ready")


class _InitProtocolErrorClient:
    """status 返回 unindexed，但 init 抛协议错误（畸形 payload 场景）。"""

    def __init__(self) -> None:
        from app.codegraph import CodeGraphProtocolIncompatibleError

        self._err = CodeGraphProtocolIncompatibleError("malformed init payload")

    def index_status(self, workspace_path: str, timeout: float | None = None) -> IndexStatusResult:
        return IndexStatusResult(state="unindexed", last_indexed_at=None)

    def index_init(self, workspace_path: str, timeout: float | None = None):
        raise self._err


def _svc(client) -> CodeGraphLifecycleService:
    return CodeGraphLifecycleService(client)


def test_ensure_ready_unindexed_triggers_init():
    client = _FakeClient(IndexStatusResult(state="unindexed", last_indexed_at=None))
    result = _svc(client).ensure_ready("/ws/a")
    assert result.ready is True
    assert result.state == "ready"
    assert result.action_taken == "init"
    assert result.files_changed == 42
    assert result.degraded_reason is None
    assert client.init_calls == ["/ws/a"]
    assert client.sync_calls == []


def test_ensure_ready_ready_triggers_sync():
    client = _FakeClient(IndexStatusResult(state="ready", last_indexed_at=123))
    result = _svc(client).ensure_ready("/ws/a")
    assert result.ready is True
    assert result.action_taken == "sync"
    # sync 摘要：added + modified + removed = 1 + 2 + 0 = 3
    assert result.files_changed == 3
    assert client.sync_calls == ["/ws/a"]
    assert client.init_calls == []


def test_ensure_ready_indexing_degrades():
    client = _FakeClient(IndexStatusResult(state="indexing", last_indexed_at=None))
    result = _svc(client).ensure_ready("/ws/a")
    assert result.ready is False
    assert result.state == "failed"
    assert "being built" in (result.degraded_reason or "")
    assert client.init_calls == []
    assert client.sync_calls == []


def test_ensure_ready_failed_state_degrades():
    client = _FakeClient(IndexStatusResult(state="failed", last_indexed_at=None))
    result = _svc(client).ensure_ready("/ws/a")
    assert result.ready is False
    assert result.state == "failed"
    assert "rebuild" in (result.degraded_reason or "")


def test_ensure_ready_kernel_unavailable_degrades_not_raise():
    client = _UnavailableClient()
    result = _svc(client).ensure_ready("/ws/a")
    assert result.ready is False
    assert result.state == "unavailable"
    assert result.degraded_reason is not None


def test_get_client_returns_injected_client():
    """新增的 get_client() 应返回构造时注入的底层 client（供 prepare 前健康快检使用）。"""
    client = _FakeClient(IndexStatusResult(state="ready", last_indexed_at=1))
    svc = _svc(client)
    assert svc.get_client() is client


def test_get_client_returns_none_when_not_injected():
    """未注入 client 时 get_client() 返回 None（prepare 应据此直接降级）。"""
    svc = CodeGraphLifecycleService(None)
    assert svc.get_client() is None


def test_ensure_ready_init_protocol_error_degrades_not_raise():
    """init 抛协议错误（畸形 payload）时，ensure_ready 应降级而非抛异常。"""
    client = _InitProtocolErrorClient()
    result = _svc(client).ensure_ready("/ws/a")
    assert result.ready is False
    assert result.state == "unavailable"
    assert "index action failed" in (result.degraded_reason or "")


def test_ensure_ready_same_workspace_singleflight():
    """同 workspace 并发 ensure_ready 只执行一次 init（InflightRegistry 去重）。"""
    status = IndexStatusResult(state="unindexed", last_indexed_at=None)
    client = _FakeClient(status)
    svc = _svc(client)
    results: list[WorkspaceReadiness] = []

    def run() -> None:
        results.append(svc.ensure_ready("/ws/concurrent"))

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(client.init_calls) == 1
    assert all(r.ready is True for r in results)
    assert all(r.action_taken == "init" for r in results)


# ----------------------------------------------------------------------
# InflightRegistry 独立测试
# ----------------------------------------------------------------------


def test_inflight_registry_deduplicates_by_key():
    registry = InflightRegistry()
    calls: list[int] = []
    lock = threading.Lock()

    def work(n: int):
        def fn() -> int:
            with lock:
                calls.append(1)
            time.sleep(0.05)
            return n

        return fn

    results: list[int] = []

    def run(n: int) -> None:
        results.append(registry.run("k", work(n)))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # 同 key 并发只执行一次 fn。
    assert len(calls) == 1
    # 所有等待者拿到 leader 的同一结果（leader 具体是哪个线程不确定，故只断言一致）。
    assert len(results) == 3
    assert len(set(results)) == 1


def test_inflight_registry_different_keys_run_independently():
    registry = InflightRegistry()
    calls: list[str] = []

    def work(key: str):
        def fn() -> str:
            calls.append(key)
            return key

        return fn

    a = registry.run("a", work("a"))
    b = registry.run("b", work("b"))
    assert a == "a" and b == "b"
    assert sorted(calls) == ["a", "b"]


def test_inflight_registry_reuses_after_completion():
    """调用结束后移除 key，后续同 key 调用应重新执行（不缓存结果）。"""
    registry = InflightRegistry()
    calls: list[int] = []

    def fn() -> int:
        calls.append(1)
        return 99

    assert registry.run("k", fn) == 99
    assert registry.run("k", fn) == 99
    assert len(calls) == 2


def test_inflight_registry_propagates_error():
    registry = InflightRegistry()

    def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        registry.run("k", boom)
    # 失败后 key 已移除，可再次执行（不残留）。
    assert registry.run("k", lambda: "ok") == "ok"


# ----------------------------------------------------------------------
# Client index_* 薄封装解析
# ----------------------------------------------------------------------


def test_client_index_status_parses_null_last_indexed():
    proc = _FakeProc()
    client = CodeGraphKernelClient(proc, timeout_seconds=5)
    result = client._index_status_result({"state": "unindexed", "last_indexed_at": None})
    assert result.state == "unindexed"
    assert result.last_indexed_at is None


def test_client_index_status_parses_last_indexed_at():
    proc = _FakeProc()
    client = CodeGraphKernelClient(proc, timeout_seconds=5)
    result = client._index_status_result({"state": "ready", "last_indexed_at": 1234})
    assert result.state == "ready"
    assert result.last_indexed_at == 1234


def test_client_index_init_parses():
    proc = _FakeProc()
    client = CodeGraphKernelClient(proc, timeout_seconds=5)
    result = client._index_init_result({"state": "ready", "files_indexed": 9, "duration_ms": 4})
    assert result.state == "ready"
    assert result.files_indexed == 9
    assert result.duration_ms == 4


def test_client_index_sync_parses():
    proc = _FakeProc()
    client = CodeGraphKernelClient(proc, timeout_seconds=5)
    result = client._index_sync_result(
        {
            "state": "ready",
            "files_added": 1,
            "files_modified": 2,
            "files_removed": 0,
            "duration_ms": 3,
        }
    )
    assert result.state == "ready"
    assert result.files_added == 1
    assert result.files_modified == 2
    assert result.files_removed == 0
    assert result.duration_ms == 3


def test_client_index_init_malformed_raises_protocol_error():
    """畸形 payload 应归为协议错误（非裸 KeyError），保证 ensure_ready 不逃逸裸异常。"""
    from app.codegraph import CodeGraphProtocolIncompatibleError

    proc = _FakeProc()
    client = CodeGraphKernelClient(proc, timeout_seconds=5)
    with pytest.raises(CodeGraphProtocolIncompatibleError):
        client._index_init_result({"state": "ready"})  # 缺 files_indexed
    with pytest.raises(CodeGraphProtocolIncompatibleError):
        client._index_sync_result("not-a-dict")


class _FakeStdin:
    def write(self, _data: str) -> int:
        return len(_data)

    def flush(self) -> None:
        return None


class _FakeProc:
    stdin = _FakeStdin()
    stdout = None
    stderr = None

    def poll(self) -> None:
        return None
