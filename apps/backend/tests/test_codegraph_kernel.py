"""CodeGraph Kernel 后端子系统单元测试（对齐设计文档 T10）。

覆盖：
- 协议错误码 → Python 异常映射（error_from_code）；
- 锁定 node 解析（resolve_node_binary）：环境变量覆盖 / 固定目录缺失报错；
- Client 的 JSON-line 解析与错误映射（_dispatch_line）。

不覆盖：真实进程启停（归 T9 冒烟脚本，避免测试依赖 node 运行时）。
"""

import concurrent.futures

from app.codegraph import (
    CodeGraphKernelClient,
    CodeGraphKernelError,
    CodeGraphKernelTimeoutError,
    CodeGraphKernelUnavailableError,
    CodeGraphNodeMissingError,
    CodeGraphProtocolIncompatibleError,
    CodeGraphToolNotAllowedError,
    CodeGraphWorkspaceNotIndexedError,
    KernelErrorCode,
    error_from_code,
    resolve_node_binary,
)


class _FakeStdin:
    """吞掉写入、flush 无操作的假 stdin，供 Client.call 使用。"""

    def write(self, _data: str) -> int:
        return len(_data)

    def flush(self) -> None:
        return None


class _FakeProc:
    """stdout=None 的假进程：使 Client 的 reader 线程立即退出，不阻塞测试。"""

    stdin = _FakeStdin()
    stdout = None
    stderr = None

    def poll(self) -> None:
        return None


def test_error_from_code_maps_each_code():
    cases = [
        (KernelErrorCode.PROTOCOL_INCOMPATIBLE, CodeGraphProtocolIncompatibleError, False),
        (KernelErrorCode.KERNEL_UNAVAILABLE, CodeGraphKernelUnavailableError, True),
        (KernelErrorCode.TIMEOUT, CodeGraphKernelTimeoutError, True),
        (KernelErrorCode.WORKSPACE_NOT_INDEXED, CodeGraphWorkspaceNotIndexedError, False),
        (KernelErrorCode.TOOL_NOT_ALLOWED, CodeGraphToolNotAllowedError, False),
        (KernelErrorCode.INTERNAL, CodeGraphKernelError, False),
    ]
    for code, exc_type, retryable in cases:
        err = error_from_code(code, "boom", retryable)
        assert isinstance(err, exc_type)
        assert err.retryable is retryable
        assert "boom" in str(err)


def test_resolve_node_binary_uses_explicit_override(tmp_path, monkeypatch):
    # 造一个临时文件作为覆盖目标，避免硬编码本机路径导致不可重复。
    fake_node = tmp_path / "node.exe"
    fake_node.write_text("")
    monkeypatch.setenv("CODING_AGENT_CODEGRAPH_NODE", str(fake_node))
    # 固定目录不存在，覆盖应优先返回。
    path = resolve_node_binary()
    assert str(path) == str(fake_node)


def test_resolve_node_binary_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("CODING_AGENT_CODEGRAPH_NODE", raising=False)
    # 固定目录可能已存在（如本地验证时放了 node.exe），故 mock 候选路径指向
    # 不存在的临时目录，使「缺失」前提不依赖真实文件系统状态。
    from app.codegraph import node_resolver

    missing_dir = tmp_path / "no-node"
    monkeypatch.setattr(node_resolver, "_fixed_node_candidates", lambda: [missing_dir / "node.exe"])
    try:
        resolve_node_binary()
        raise AssertionError("expected CodeGraphNodeMissingError")
    except CodeGraphNodeMissingError as exc:
        assert "locked node not found" in str(exc)


def test_client_dispatch_line_success():
    client = CodeGraphKernelClient(_FakeProc(), timeout_seconds=5)
    future: concurrent.futures.Future = concurrent.futures.Future()
    client._pending["req-1"] = future
    client._dispatch_line('{"id":"req-1","result":{"ok":true}}')
    assert future.result(timeout=1) == {"ok": True}


def test_client_dispatch_line_error_maps_to_exception():
    client = CodeGraphKernelClient(_FakeProc(), timeout_seconds=5)
    future: concurrent.futures.Future = concurrent.futures.Future()
    client._pending["req-2"] = future
    client._dispatch_line(
        '{"id":"req-2","error":{"code":"WORKSPACE_NOT_INDEXED",'
        '"message":"no index","retryable":false}}'
    )
    assert future.done()
    err = future.exception()
    assert isinstance(err, CodeGraphWorkspaceNotIndexedError)
    assert err.retryable is False


def test_client_dispatch_line_invalid_json_ignored():
    client = CodeGraphKernelClient(_FakeProc(), timeout_seconds=5)
    future: concurrent.futures.Future = concurrent.futures.Future()
    client._pending["req-3"] = future
    # 非法 JSON 不应投递，Future 保持未完成。
    client._dispatch_line("not-json-at-all")
    assert not future.done()
    client.stop()


def test_client_dispatch_line_unknown_code_falls_back_to_internal():
    client = CodeGraphKernelClient(_FakeProc(), timeout_seconds=5)
    future: concurrent.futures.Future = concurrent.futures.Future()
    client._pending["req-4"] = future
    client._dispatch_line(
        '{"id":"req-4","error":{"code":"TOTALLY_UNKNOWN","message":"x","retryable":true}}'
    )
    assert isinstance(future.exception(), CodeGraphKernelError)


def test_client_dispatch_line_is_error_field_read_correctly():
    # 回归：server 写蛇形 is_error，client 必须读 is_error（曾因驼峰 isError 漏读恒 false）。
    client = CodeGraphKernelClient(_FakeProc(), timeout_seconds=5)
    future: concurrent.futures.Future = concurrent.futures.Future()
    client._pending["req-5"] = future
    client._dispatch_line(
        '{"id":"req-5","result":{"content":[{"type":"text","text":"nope"}],"is_error":true}}'
    )
    result = future.result(timeout=1)
    assert result == {"content": [{"type": "text", "text": "nope"}], "is_error": True}


def test_client_pending_lock_prevents_change_during_iteration():
    # 回归：reader 线程迭代 pending 时调用线程插入新 key 不应抛 RuntimeError。
    client = CodeGraphKernelClient(_FakeProc(), timeout_seconds=5)
    fut: concurrent.futures.Future = concurrent.futures.Future()
    client._pending["a"] = fut
    # 模拟 reader 快照后调用线程插入：_fail_all_pending 先快照再失败化，插入安全。
    import threading

    def inserter() -> None:
        with client._pending_lock:
            client._pending["b"] = concurrent.futures.Future()

    t = threading.Thread(target=inserter)
    t.start()
    client._fail_all_pending()
    t.join(timeout=1)
    # 原 pending 被失败化；插入的新 key 不抛 RuntimeError（迭代用快照保护）。
    assert fut.done()
    assert "b" in client._pending
