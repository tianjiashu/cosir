"""MCP 延迟工具的端到端：真实 stdio MCP 服务、真实 actor / workflow / SQLite / projector，只回放模型响应。

覆盖完整链路上「已加载」这件事实的去留：检索加载 → 下一次请求带上定义 → 调用到真实服务；同一批里先加载
后调用；重启（运行时空间与连接池都重建）后照样可用；分叉继承；撤回加载的回合即卸载；服务下线时目录通知与
调用报错。每个场景都核对实时快照与冷重建一致。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.service import depends
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import MCPServerConfig, ScopedMCPServer
from app.service.configuration.mcp.machine_state import MCPMachineState
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces as spaces
from app.storage.model.session_context_model import SessionContextModel
from app.storage.store_engines import main_db_factory
from app.tools.mcp.client import list_server_tools
from app.tools.mcp.connection_pool import MCPConnectionPool
from app.utils.path import system_cosir
from tests.live_cold_support import LiveColdEnv, tool_call, tool_parts

ECHO = "mcp__echo__echo"
_SERVER_CODE = (
    "from mcp.server.fastmcp import FastMCP\n"
    "server = FastMCP('echo-server')\n"
    "@server.tool(description='Echo a value back')\n"
    "def echo(value: str) -> dict:\n"
    "    return {'echo': value}\n"
    "server.run(transport='stdio')\n"
)


def _search(call_id: str) -> tuple[list[dict[str, Any]], str]:
    args = {"intent": "加载回显工具", "query": f"select:{ECHO}", "max_results": 5}
    return [{"tool_calls": [tool_call(call_id, "tool_search", args)]}], "tool_calls"


def _echo(call_id: str, value: str = "hi") -> tuple[list[dict[str, Any]], str]:
    return [{"tool_calls": [tool_call(call_id, ECHO, {"intent": "回显", "value": value})]}], "tool_calls"


def _done(text: str = "完成") -> tuple[list[dict[str, Any]], str]:
    return [{"content": text}], "stop"


def _write_config(workspace: Path, *, enabled: bool = True) -> MCPServerConfig:
    config = MCPServerConfig(
        name="echo", command=sys.executable, args=["-c", _SERVER_CODE], enabled=enabled
    )
    path = workspace / ".cosir" / "config" / "mcp_servers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": [config.model_dump(mode="json")]}), encoding="utf-8")
    return config


@pytest.fixture
def mcp_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """工作区里一个配置好、像配置页那样测试通过的 stdio 服务，和一个用例独占的连接池。"""
    monkeypatch.setattr(system_cosir, "LOG_DIR", tmp_path / "logs")
    server = ScopedMCPServer(ExtensionScope.workspace(tmp_path), _write_config(tmp_path))
    MCPMachineState.record_pass(server, asyncio.run(list_server_tools(server)))
    return tmp_path


@pytest.fixture
def pools():
    """可整个换掉的连接池：模拟重启时连接全部丢失。

    自带补丁并在本夹具收尾时先撤掉：服务依赖的复位要按缓存信息关闭真正的单例，替身没有它。
    """
    created: list[MCPConnectionPool] = []
    patch = pytest.MonkeyPatch()

    def renew() -> MCPConnectionPool:
        pool = MCPConnectionPool()
        created.append(pool)
        patch.setattr("app.service.depends.get_mcp_connection_pool", lambda: pool)
        return pool

    renew()
    yield renew
    patch.undo()
    for pool in created:
        pool.close()


def _tool_message(env: LiveColdEnv, call_id: str) -> str:
    for request in reversed(env.model.requests):
        for message in request:
            if message["role"] == "tool" and message["tool_call_id"] == call_id:
                return message["content"]
    raise AssertionError(f"no tool message for {call_id}")


def _part(env: LiveColdEnv, call_id: str, session_id: int | None = None) -> dict[str, Any]:
    state = env.live() if session_id is None else env.cold(session_id)
    return next(part for part in tool_parts(state) if part["toolCallId"] == call_id)


async def _next_run(env: LiveColdEnv, previous_id: int, session_id: int | None = None) -> Any:
    """等新开的 Run 落定：``parked`` 只看当前 Run，新消息还没开出 Run 时会立刻返回上一个。"""
    async with asyncio.timeout(10):
        while True:
            run = await env.parked(session_id)
            if run.id != previous_id:
                return run
            await asyncio.sleep(0.02)


async def _latest_input_id(session_id: int) -> int:
    def query() -> int:
        with main_db_factory()() as db:
            rows = db.scalars(
                select(SessionContextModel)
                .where(SessionContextModel.session_id == session_id)
                .order_by(SessionContextModel.sequence)
            ).all()
        return next(
            row.message_id
            for row in reversed(rows)
            if json.loads(row.message_json)["role"] == "user"
            and json.loads(row.metadata_json).get("kind") == "user_input"
            and row.include_in_context
            and row.message_id
        )

    return await asyncio.to_thread(query)


async def test_search_loads_the_tool_for_the_next_request_and_calls_the_real_server(
    backend_env, mcp_workspace, pools, monkeypatch
):
    env = LiveColdEnv(mcp_workspace, monkeypatch, [_search("c1"), _echo("c2"), _done()])
    try:
        env.send()
        run = await env.parked()

        assert run.status == "completed"
        # 首个请求只有名字目录（在系统提示词里），没有定义；检索之后的请求才带上它。
        assert ECHO not in env.model.tool_names[0] and "tool_search" in env.model.tool_names[0]
        assert ECHO in env.model.requests[0][0]["content"]
        assert env.model.tool_names[1][-1] == ECHO
        assert _tool_message(env, "c1").startswith("<functions>\n<function>{")
        assert '"echo": "hi"' in _tool_message(env, "c2")
        echo = _part(env, "c2")
        assert echo["status"] == "completed"
        assert echo["display_data"]["kind"] == "mcp-tool-result"
        assert (echo["display_data"]["server_name"], echo["display_data"]["tool_name"]) == ("echo", "echo")
        env.assert_parity()
    finally:
        await env.close()


async def test_calling_in_the_same_batch_as_the_search_is_rejected_then_works(
    backend_env, mcp_workspace, pools, monkeypatch
):
    search_and_call = (
        [
            {
                "tool_calls": [
                    tool_call("c1", "tool_search", {"intent": "加载", "query": f"select:{ECHO}", "max_results": 5}),
                    tool_call("c2", ECHO, {"intent": "回显", "value": "early"}, index=1),
                ]
            }
        ],
        "tool_calls",
    )
    env = LiveColdEnv(mcp_workspace, monkeypatch, [search_and_call, _echo("c3"), _done()])
    try:
        env.send()
        await env.parked()

        assert f'"select:{ECHO}"' in _tool_message(env, "c2")
        assert _part(env, "c2")["status"] == "failed"
        assert _part(env, "c3")["status"] == "completed"
        env.assert_parity()
    finally:
        await env.close()


async def test_loaded_tools_survive_a_restart_and_reconnect(
    backend_env, mcp_workspace, pools, monkeypatch
):
    env = LiveColdEnv(
        mcp_workspace, monkeypatch, [_search("c1"), _done("加载好了"), _echo("c2", "after"), _done()]
    )
    try:
        env.send()
        first = await env.parked()
        # 重启：运行时空间（上下文工作副本、actor）全部丢掉，连接池换新，已加载的事实只剩库里的上下文。
        await spaces.aclose()
        spaces.bind_loop(asyncio.get_running_loop())
        pools()
        env.send("再来")
        run = await _next_run(env, first.id)

        assert run.status == "completed"
        assert ECHO in env.model.tool_names[2]
        assert '"echo": "after"' in _tool_message(env, "c2")
    finally:
        await env.close()


async def test_a_manual_fork_inherits_the_loaded_tools(
    backend_env, mcp_workspace, pools, monkeypatch
):
    env = LiveColdEnv(
        mcp_workspace, monkeypatch, [_search("c1"), _done("加载好了"), _echo("c2", "fork"), _done()]
    )
    try:
        env.send()
        await env.parked()
        fork = await depends.get_session_service().fork_session(env.session.id, turn=1)
        env.send("在分支里用", session_id=fork.id)
        run = await env.parked(fork.id)

        assert run.status == "completed"
        assert ECHO in env.model.tool_names[2]
        assert '"echo": "fork"' in _tool_message(env, "c2")
        assert _part(env, "c2", fork.id)["status"] == "completed"
    finally:
        await env.close()


async def test_withdrawing_the_loading_turn_unloads_the_tool(
    backend_env, mcp_workspace, pools, monkeypatch
):
    env = LiveColdEnv(
        mcp_workspace, monkeypatch, [_search("c1"), _done("加载好了"), _echo("c2"), _done("没加载")]
    )
    try:
        env.send()
        first = await env.parked()
        await depends.get_assistant_transport_service().withdraw(
            env.session.id, await _latest_input_id(env.session.id)
        )
        env.send("重新问")
        await _next_run(env, first.id)

        assert ECHO not in env.model.tool_names[2]
        assert f'"select:{ECHO}"' in _tool_message(env, "c2")
        assert _part(env, "c2")["status"] == "failed"
        env.assert_parity()
    finally:
        await env.close()


async def test_a_server_going_away_is_announced_and_its_loaded_tool_refuses_calls(
    backend_env, mcp_workspace, pools, monkeypatch
):
    env = LiveColdEnv(
        mcp_workspace, monkeypatch, [_search("c1"), _done("加载好了"), _echo("c2"), _done("服务没了")]
    )
    try:
        env.send()
        first = await env.parked()
        _write_config(mcp_workspace, enabled=False)
        env.send("还能用吗")
        await _next_run(env, first.id)

        second_turn = env.model.requests[2]
        notices = [m["content"] for m in second_turn if m["role"] == "user" and "<system_notice" in str(m["content"])]
        assert any("no longer available. Do not search for them" in str(n) and ECHO in str(n) for n in notices)
        # 加载过的定义仍在请求里（不改请求前缀），调用时按当前配置拒绝。
        assert ECHO in env.model.tool_names[2]
        assert "no longer available" in _tool_message(env, "c2")
        assert _part(env, "c2")["status"] == "failed"
        env.assert_parity()
    finally:
        await env.close()
