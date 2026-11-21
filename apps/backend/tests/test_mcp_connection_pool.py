"""MCP 长连接池：连接复用、指纹变化重建、断线重连、空闲回收与调用取消。

用真实 stdio 服务验证：服务进程内的计数器在同一连接上递增，换了连接就从头开始，以此区分
「复用」与「重建」。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import MCPServerConfig, ScopedMCPServer
from app.service.configuration.mcp.machine_state import MCPMachineState, MCPServerCatalog
from app.tools.mcp.call_result import MCPCallResult
from app.tools.mcp.client import MCPToolNotFoundError, is_connection_failure
from app.tools.mcp.connection_pool import MCPConnectionPool
from app.tools.mcp.mcp_tools import MCPExternalTools
from app.utils.path import system_cosir

_SERVER_CODE = """
import os, sys
import anyio
from mcp.server.fastmcp import FastMCP

server = FastMCP("stateful")
count = 0

@server.tool()
def bump() -> dict:
    global count
    count += 1
    print(f"bump {count}", file=sys.stderr, flush=True)
    return {"count": count, "pid": os.getpid()}

@server.tool()
def crash() -> str:
    os._exit(1)

@server.tool()
async def slow(seconds: float) -> str:
    await anyio.sleep(seconds)
    return "done"

server.run(transport="stdio")
"""


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(system_cosir, "LOG_DIR", tmp_path / "logs")
    pool = MCPConnectionPool()
    yield pool
    pool.close()


def _server(name: str = "stateful", *extra_args: str) -> ScopedMCPServer:
    return ScopedMCPServer(
        ExtensionScope.system(),
        MCPServerConfig(name=name, command=sys.executable, args=["-c", _SERVER_CODE, *extra_args]),
    )


def _bump(pool: MCPConnectionPool, server: ScopedMCPServer) -> dict:
    result = pool.submit(pool.call_tool(server, "bump", {})).result(timeout=30)
    return json.loads(MCPCallResult.from_sdk(result).text)


def test_calls_reuse_one_connection_and_server_process(pool, tmp_path):
    server = _server()

    first = _bump(pool, server)
    second = _bump(pool, server)

    assert (first["count"], second["count"]) == (1, 2)
    assert first["pid"] == second["pid"]
    stderr_log = tmp_path / "logs" / "mcp" / "stateful.stderr.log"
    assert "bump 2" in stderr_log.read_text(encoding="utf-8")


def test_changed_connection_fingerprint_rebuilds_connection(pool):
    first = _bump(pool, _server())
    # 多一个被服务忽略的参数即改变连接指纹：配置变了就必须换连接。
    rebuilt = _bump(pool, _server("stateful", "--changed"))

    assert rebuilt["count"] == 1
    assert rebuilt["pid"] != first["pid"]


def test_lost_connection_fails_once_then_reconnects(pool):
    server = _server()
    first = _bump(pool, server)

    with pytest.raises(Exception) as crashed:
        pool.submit(pool.call_tool(server, "crash", {})).result(timeout=30)
    assert is_connection_failure(crashed.value)
    recovered = _bump(pool, server)

    assert recovered["count"] == 1
    assert recovered["pid"] != first["pid"]


def test_idle_connections_are_reaped(pool):
    server = _server()
    first = _bump(pool, server)

    pool.submit(pool.reap_idle(idle_seconds=0)).result(timeout=30)
    after_reap = _bump(pool, server)

    assert after_reap["count"] == 1
    assert after_reap["pid"] != first["pid"]


def test_unknown_tool_is_reported_without_dropping_connection(pool):
    server = _server()
    first = _bump(pool, server)

    with pytest.raises(MCPToolNotFoundError):
        pool.submit(pool.call_tool(server, "missing", {})).result(timeout=30)

    assert _bump(pool, server)["pid"] == first["pid"]


def _make_available(server: ScopedMCPServer, pool: MCPConnectionPool, tmp_path, monkeypatch) -> None:
    """写进系统级配置并记一次测试通过，让 MCP 工具能选中它，且用这个池。"""

    config_path = tmp_path / "system-cosir" / "config" / "mcp_servers.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"servers": [server.config.model_dump(mode="json")]}), encoding="utf-8"
    )
    MCPMachineState.record_pass(server, MCPServerCatalog())
    monkeypatch.setattr("app.service.depends.get_mcp_connection_pool", lambda: pool)


def _tool_context(tool_call_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=9001,
        workspace_id=1,
        workspace_root=Path("."),
        step_id="step-1",
        run_id=1,
        tool_call_id=tool_call_id,
    )


def _slow_start_server(seconds: float) -> ScopedMCPServer:
    """启动先睡一会儿再握手的服务，模拟冷启动慢的 ``npx`` 类服务。"""

    code = f"import time\ntime.sleep({seconds})\n{_SERVER_CODE}"
    return ScopedMCPServer(
        ExtensionScope.system(),
        MCPServerConfig(name="slowstart", command=sys.executable, args=["-c", code]),
    )


def test_cancelled_tool_call_returns_promptly_and_keeps_connection(pool, tmp_path, monkeypatch):
    server = _server()
    _make_available(server, pool, tmp_path, monkeypatch)
    first = _bump(pool, server)
    context = _tool_context("call-slow")
    threading.Timer(
        0.5, tool_call_cancellation_registry.mark_cancelled, args=(9001, "call-slow")
    ).start()

    started = time.monotonic()
    try:
        observation = MCPExternalTools()._call(
            "mcp__stateful__slow", server, "slow", {"seconds": 30}, context
        )
    finally:
        tool_call_cancellation_registry.clear(9001, "call-slow")

    assert observation.status == "cancelled"
    # 请求已经发给服务，停下只是不再等：不能说成「没有结果」，模型才不会把副作用重做一遍。
    assert "may already have acted on it" in observation.content
    assert time.monotonic() - started < 5
    assert _bump(pool, server)["pid"] == first["pid"]


# 潜在缺陷：建连超时与调用超时都是 TimeoutError，建连没成功也被说成「请求已放弃，服务可能已执行」。
def test_connect_timeout_is_reported_as_not_sent(pool, tmp_path, monkeypatch):
    server = _slow_start_server(3)
    _make_available(server, pool, tmp_path, monkeypatch)
    monkeypatch.setattr("app.tools.mcp.connection_pool.CONNECT_TIMEOUT_SECONDS", 0.5)

    observation = MCPExternalTools()._call(
        "mcp__stateful__bump", server, "bump", {}, _tool_context("call-connect")
    )

    assert observation.status == "error"
    assert "did not respond in time" in observation.content
    assert "may still have acted" not in observation.content
    assert observation.display_data == {"status_hint": "连接超时"}


# 潜在缺陷：列目录给「建连 + 列目录」整体的时限比建连时限短，冷启动慢的服务永远列不出目录。
def test_slow_starting_server_can_still_be_listed(pool, tmp_path, monkeypatch):
    server = _slow_start_server(1.5)
    _make_available(server, pool, tmp_path, monkeypatch)
    monkeypatch.setattr("app.tools.mcp.connection_pool.LIST_TOOLS_TIMEOUT_SECONDS", 0.5)

    tools = pool.submit(pool.list_tools(server)).result(timeout=30)

    assert {tool.name for tool in tools} >= {"bump", "slow"}
    # 连接池列到的目录回写本机记录：Agent 的延迟工具目录只读那份记录。
    assert {entry.name for entry in MCPMachineState.read().catalog(server).tools} >= {"bump", "slow"}


def test_same_name_in_another_scope_gets_its_own_connection(pool, tmp_path):
    """连接按「作用域 + 服务名」区分：工作区的同名服务不会顶替系统级连接，反之亦然。"""

    system_server = _server()
    workspace_server = ScopedMCPServer(
        ExtensionScope.workspace(tmp_path), _server("stateful", "--workspace").config
    )

    system_first = _bump(pool, system_server)
    workspace_first = _bump(pool, workspace_server)
    system_second = _bump(pool, system_server)

    assert (system_first["count"], workspace_first["count"], system_second["count"]) == (1, 1, 2)
    assert system_second["pid"] == system_first["pid"]
    assert workspace_first["pid"] != system_first["pid"]


def test_closed_pool_rejects_new_work(pool):
    pool.close()

    with pytest.raises(RuntimeError, match="closed"):
        pool.submit(pool.reap_idle())


def test_closing_the_pool_ends_the_stdio_server_process(pool):
    """目的：后端退出时关闭连接池（``close_service_dependencies``），stdio 服务子进程随之结束。
    潜在缺陷：Windows 上只关管道不结束进程，服务子进程留在后台占着资源。"""

    from tests.process_tree_support import kill, survivors

    pid = _bump(pool, _server())["pid"]

    pool.close()

    left = survivors([pid], timeout=10)
    for survivor in left:
        kill(survivor)
    assert left == [], "stdio MCP server survived the pool"


def test_stdio_server_exits_when_the_backend_holding_it_is_killed(tmp_path):
    """目的：持有连接池的后端被强杀（没机会关闭连接池）时，stdio 服务读到 stdin EOF 自行退出。"""

    from tests.process_tree_support import kill, survivors

    script = f"""
import json, sys, time
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import MCPServerConfig, ScopedMCPServer
from app.tools.mcp.call_result import MCPCallResult
from app.tools.mcp.connection_pool import MCPConnectionPool
from app.utils.path import system_cosir
from pathlib import Path
system_cosir.LOG_DIR = Path({str(tmp_path / "logs")!r})
server = ScopedMCPServer(
    ExtensionScope.system(),
    MCPServerConfig(name="stateful", command=sys.executable, args=["-c", {_SERVER_CODE!r}]),
)
pool = MCPConnectionPool()
result = pool.submit(pool.call_tool(server, "bump", {{}})).result(timeout=30)
print("SERVER_PID:" + str(json.loads(MCPCallResult.from_sdk(result).text)["pid"]), flush=True)
time.sleep(60)
"""
    backend = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert backend.stdout is not None
        line = backend.stdout.readline()
        assert line.startswith("SERVER_PID:"), line
        pid = int(line.split(":", 1)[1])

        kill(backend.pid)
        backend.wait(10)

        left = survivors([pid], timeout=10)
        for survivor in left:
            kill(survivor)
        assert left == [], "stdio MCP server survived the killed backend"
    finally:
        if backend.poll() is None:
            backend.kill()
