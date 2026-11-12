"""``tool_call_id`` 的约定：模型给什么都行，进入持久化后在 Task 内唯一。

全系统（决定与取消入口、占位结算、看板、前端开合）都按 id 寻址，厂商重复 id 不能让这些入口串线。
不同 Task 之间 id 可以相同，进程级登记表因此按 ``(task_id, tool_call_id)`` 寻址。
"""

import asyncio

from app.core.tool.handoff.handoff_registry import HandoffLease, HandoffRegistry
from app.service import depends
from app.service.conversation.tool_decision_service import ToolDecisionService
from app.tools.tool_handler.terminal.command_results import command_results
from tests.live_cold_support import GatedCommands, LiveColdEnv, tool_call, tool_parts


def _read(path: str, intent: str = "读") -> dict[str, str]:
    return {"intent": intent, "path": path}


def _command(name: str) -> dict[str, object]:
    return {"intent": name, "command": name, "background": True}


async def test_same_response_duplicates_both_run_and_the_task_stays_usable(
    backend_env, tmp_path, monkeypatch
):
    write = {"intent": "写", "content": "x"}
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [
            (
                [
                    {
                        "tool_calls": [
                            tool_call("d", "write_file", {**write, "path": "a.txt"}, 0),
                            tool_call("d", "write_file", {**write, "path": "b.txt"}, 1),
                        ]
                    }
                ],
                "tool_calls",
            ),
            ([{"content": "第一轮"}], "stop"),
            ([{"content": "第二轮"}], "stop"),
        ],
    )
    try:
        env.send("一")
        assert (await env.until("completed")).final_output == "第一轮"
        assert [part["toolCallId"] for part in tool_parts(env.live())] == ["d", "d_2"]
        env.assert_parity()
        env.send("二")
        await asyncio.sleep(0.2)
        assert (await env.until("completed")).final_output == "第二轮"
    finally:
        await env.close()


async def test_later_response_reusing_an_id_keeps_both_cards(backend_env, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [
            ([{"tool_calls": [tool_call("call_0", "read_file", _read("a.txt", "读a"))]}], "tool_calls"),
            ([{"tool_calls": [tool_call("call_0", "read_file", _read("b.txt", "读b"))]}], "tool_calls"),
            ([{"content": "结论"}], "stop"),
        ],
    )
    try:
        env.send()
        await env.until("completed")
        cards = [(part["toolCallId"], part["intent"]) for part in tool_parts(env.live())]
        assert cards == [("call_0", "读a"), ("call_0_2", "读b")]
        env.assert_parity()
        # 模型下一轮看到的是规范 id，工具结果与调用一一配对。
        ids = [c["id"] for m in env.model.requests[-1] for c in m.get("tool_calls") or []]
        assert ids == ["call_0", "call_0_2"]
    finally:
        await env.close()


async def test_a_reused_id_cannot_steal_the_result_of_running_background_work(
    backend_env, tmp_path, monkeypatch
):
    (tmp_path / "a.txt").write_text("x")
    commands = GatedCommands(monkeypatch, "A")
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [
            ([{"tool_calls": [tool_call("c", "execute_terminal", _command("A"))]}], "tool_calls"),
            ([{"tool_calls": [tool_call("c", "read_file", _read("a.txt"))]}], "tool_calls"),
            ([{"tool_calls": [tool_call("w", "wait_messages", {"intent": "等"})]}], "tool_calls"),
            ([{"content": "收到结论"}], "stop"),
        ],
    )
    try:
        env.send()
        assert (await env.parked()).status == "suspended"
        commands.release()
        assert (await env.until("completed")).final_output == "收到结论"
        assert {part["status"] for part in tool_parts(env.cold())} == {"completed"}
        env.assert_parity()
    finally:
        await env.close()


async def test_two_pending_decisions_with_the_same_id_are_decided_separately(
    backend_env, tmp_path, monkeypatch
):
    hitl = {"intent": "批准"}
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [
            ([{"tool_calls": [tool_call("h", "probe_hitl", hitl)]}], "tool_calls"),
            ([{"tool_calls": [tool_call("h", "probe_hitl", hitl)]}], "tool_calls"),
            ([{"content": "继续"}], "stop"),
        ],
    )
    try:
        env.send()
        await env.parked()
        env.send("继续", delivery="immediate")
        await asyncio.sleep(0.1)
        await env.parked()
        waiting = [p["toolCallId"] for p in tool_parts(env.live()) if p["awaitingUser"]]
        assert waiting == ["h", "h_2"]
        await asyncio.to_thread(ToolDecisionService().submit, env.task.id, "h", "approve")
        await asyncio.sleep(0.1)
        await env.parked()
        statuses = {p["toolCallId"]: p["status"] for p in tool_parts(env.cold())}
        assert statuses["h"] == "completed"
        assert statuses["h_2"] == "pending"
    finally:
        await env.close()


async def test_a_provider_repeating_the_id_on_every_chunk_gets_one_card(
    backend_env, tmp_path, monkeypatch
):
    """厂商每片重发 id 时 SDK 把它拼成 ``call_0call_0``；调用身份按位置冻结，不跟着变。"""
    (tmp_path / "a.txt").write_text("x")

    resent = {"index": 0, "id": "call_0", "function": {"arguments": '"path":"a.txt"}'}}
    deltas = [
        {"tool_calls": [tool_call("call_0", "read_file", '{"intent":"读",')]},
        {"tool_calls": [resent]},
    ]
    env = LiveColdEnv(
        tmp_path, monkeypatch, [(deltas, "tool_calls"), ([{"content": "结论"}], "stop")]
    )
    try:
        env.send()
        await env.until("completed")
        assert [part["toolCallId"] for part in tool_parts(env.live())] == ["call_0"]
        env.assert_parity()
    finally:
        await env.close()


async def test_same_id_in_two_tasks_runs_independently(backend_env, tmp_path, monkeypatch):
    commands = GatedCommands(monkeypatch, "A", "B")
    call = {"A": tool_call("call_0", "execute_terminal", _command("A"))}
    call["B"] = tool_call("call_0", "execute_terminal", _command("B"))
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [
            ([{"tool_calls": [call["A"]]}], "tool_calls"),
            ([{"tool_calls": [tool_call("w", "wait_messages", {"intent": "等"})]}], "tool_calls"),
            ([{"tool_calls": [call["B"]]}], "tool_calls"),
            ([{"tool_calls": [tool_call("w", "wait_messages", {"intent": "等"})]}], "tool_calls"),
            ([{"content": "x"}], "stop"),
            ([{"content": "y"}], "stop"),
        ],
    )
    try:
        env.send()
        await env.parked()
        other = depends.get_workspace_service().create_task(env.workspace.id, "other")
        env.send("另一个", task_id=other.id)
        await asyncio.sleep(0.1)
        await env.parked(other.id)
        assert list(command_results.live_background(env.task.id)) == ["call_0"]
        assert list(command_results.live_background(other.id)) == ["call_0"]
    finally:
        commands.release()
        await env.close()


def test_handoffs_with_the_same_id_stay_in_their_own_task():
    registry = HandoffRegistry()
    registry.begin(HandoffLease("call_0", 1, "browser_handoff", "t1", "r"))
    registry.begin(HandoffLease("call_0", 2, "browser_handoff", "t2", "r"))
    ended = registry.end(1, "call_0")
    assert ended is not None and ended.task_id == 1
    assert [lease.task_id for lease in registry.leases(2)] == [2]
