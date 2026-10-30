"""MCP 长连接池：连接复用、指纹变化重建、断线重连、空闲回收与调用取消。

用真实 stdio 服务验证：服务进程内的计数器在同一连接上递增，换了连接就从头开始，以此区分
「复用」与「重建」。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.service.configuration.mcp_configuration import (
    MCPServerConfig,
    server_connection_fingerprint,
)
from app.tools.mcp.client import MCPToolNotFoundError, call_result_text, is_connection_failure
from app.tools.mcp.connection_pool import MCPConnectionPool
from app.tools.tool_handler.mcp_tool import MCPTool
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


def _server(name: str = "stateful", *extra_args: str) -> MCPServerConfig:
    return MCPServerConfig(
        name=name, command=sys.executable, args=["-c", _SERVER_CODE, *extra_args]
    )


def _bump(pool: MCPConnectionPool, server: MCPServerConfig) -> dict:
    result = pool.submit(pool.call_tool(server, "bump", {})).result(timeout=30)
    return json.loads(call_result_text(result))


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


def test_cancelled_tool_call_returns_promptly_and_keeps_connection(pool, tmp_path, monkeypatch):
    server = _server()
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps(
            {
                "servers": [server.model_dump(mode="json")],
                "tested_servers": {server.name: server_connection_fingerprint(server)},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.service.configuration.mcp_configuration.system_mcp_servers_file", lambda: config_path
    )
    monkeypatch.setattr("app.service.depends.get_mcp_connection_pool", lambda: pool)
    first = _bump(pool, server)
    context = ToolExecutionContext(
        task_id=9001,
        workspace_id=1,
        workspace_root=Path("."),
        step_id="step-1",
        run_id=1,
        tool_call_id="call-slow",
    )
    threading.Timer(
        0.5, tool_call_cancellation_registry.mark_cancelled, args=(9001, "call-slow")
    ).start()

    started = time.monotonic()
    try:
        observation = MCPTool().execute(
            action="call",
            server_name=server.name,
            tool_name="slow",
            arguments={"seconds": 30},
            execution_context=context,
        )
    finally:
        tool_call_cancellation_registry.clear(9001, "call-slow")

    assert observation.status == "cancelled"
    assert time.monotonic() - started < 5
    assert _bump(pool, server)["pid"] == first["pid"]


def test_closed_pool_rejects_new_work(pool):
    pool.close()

    with pytest.raises(RuntimeError, match="closed"):
        pool.submit(pool.reap_idle())
