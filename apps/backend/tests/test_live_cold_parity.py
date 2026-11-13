"""实时投影与冷重建必须得到同一份快照：刷新页面不能改变用户看到的事实。

已知不覆盖：同一响应里正文与工具交错时，实时按到达顺序切段，冷重建合并成一段（交错顺序只是
瞬时流序，不是持久事实）。
"""

import asyncio

import pytest

from app.assistant_transport.service.assistant_transport_service import AssistantTransportService
from app.core.tool.tool_grouping import TOOL_GROUP_SEARCH
from app.core.workflow.message_queue.session_message import NoticeBody, SessionMessage
from app.models.run_options import RunOptions
from app.service.conversation.tool_decision_service import ToolDecisionService
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces as spaces
from tests.live_cold_support import LiveColdEnv, tool_call, tool_parts
from tests.sdk_fixtures import sdk_events

_READ = {"intent": "看文件", "path": "a.txt"}
_DISABLE_SEARCH = RunOptions(disabled_tool_groups=[TOOL_GROUP_SEARCH])

_SCENARIOS = {
    "plain": ([([{"content": "hello "}, {"content": "world"}], "stop")], None),
    "length": ([([{"content": "cut"}], "length")], None),
    "empty": ([([{"content": ""}], "stop")], None),
    "tool_then_final": (
        [
            ([{"content": "进展"}, {"tool_calls": [tool_call("c1", "read_file", _READ)]}], "tool_calls"),
            ([{"content": "结论"}], "stop"),
        ],
        None,
    ),
    "invalid_json_args": (
        [
            ([{"tool_calls": [tool_call("c1", "read_file", '{"intent":"x","path":')]}], "tool_calls"),
            ([{"content": "结论"}], "stop"),
        ],
        None,
    ),
    "missing_intent": (
        [
            ([{"tool_calls": [tool_call("c1", "read_file", {"path": "a.txt"})]}], "tool_calls"),
            ([{"content": "结论"}], "stop"),
        ],
        None,
    ),
    "bad_business_args": (
        [
            ([{"tool_calls": [tool_call("c1", "read_file", {"intent": "看", "nope": 1})]}], "tool_calls"),
            ([{"content": "结论"}], "stop"),
        ],
        None,
    ),
    "disabled_tool": (
        [
            ([{"tool_calls": [tool_call("c1", "read_file", _READ)]}], "tool_calls"),
            ([{"content": "结论"}], "stop"),
        ],
        _DISABLE_SEARCH,
    ),
    "continuation": ([([{"content": "半截"}], None), ([{"content": "完整"}], "stop")], None),
    "stream_failure": (
        [
            (
                [{"content": "半截"}, {"tool_calls": [tool_call("c1", "read_file", _READ)]}],
                None,
                RuntimeError("boom"),
            )
        ],
        None,
    ),
    "model_error": ([RuntimeError("boom")], None),
}


@pytest.mark.parametrize("name", list(_SCENARIOS))
async def test_run_outcomes_agree(backend_env, tmp_path, monkeypatch, name):
    steps, options = _SCENARIOS[name]
    (tmp_path / "a.txt").write_text("x")
    env = LiveColdEnv(tmp_path, monkeypatch, steps)
    try:
        env.send(options=options)
        await env.parked()
        env.assert_parity()
    finally:
        await env.close()


async def test_a_pending_decision_survives_a_failed_run(backend_env, tmp_path, monkeypatch):
    """决定在 Run 失败后仍可作出：卡片必须还在等，不能只在实时快照里变成已取消。"""
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [
            ([{"tool_calls": [tool_call("h1", "probe_hitl", {"intent": "批准"})]}], "tool_calls"),
            RuntimeError("model down"),
            ([{"content": "结论"}], "stop"),
        ],
    )
    try:
        env.send()
        assert (await env.parked()).status == "suspended"
        env.send("唤醒", delivery="immediate")
        await env.until("failed")
        [part] = tool_parts(env.live())
        assert (part["status"], part["awaitingUser"]) == ("pending", True)
        env.assert_parity()

        await asyncio.to_thread(ToolDecisionService().submit, env.session.id, "h1", "approve")
        await env.until("completed")
        [part] = tool_parts(env.live())
        assert (part["status"], part["awaitingUser"]) == ("completed", False)
        env.assert_parity()
    finally:
        await env.close()


async def test_a_failure_during_a_tool_batch_settles_its_calls(backend_env, tmp_path, monkeypatch):
    """工具批次被异常打断：没有结果的调用落占位并投影，不靠刷新才看到结局。"""
    from app.core.workflow.nodes import tools_node

    async def explode(*_args, **_kwargs):
        raise RuntimeError("batch failed")

    monkeypatch.setattr(tools_node, "run_tool_batch", explode)
    (tmp_path / "a.txt").write_text("x")
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [([{"tool_calls": [tool_call("c1", "read_file", _READ)]}], "tool_calls")],
    )
    try:
        env.send()
        await env.until("failed")
        [part] = tool_parts(env.live())
        assert part["status"] == "cancelled"
        env.assert_parity()
    finally:
        await env.close()


async def test_a_failure_before_the_response_is_committed_settles_its_calls(
    backend_env, tmp_path, monkeypatch
):
    """流已结束、响应还是草稿时失败：调用不在上下文里，Run 的失败收尾看不到，要在节点里写成事实。"""
    from app.core.workflow.nodes import model_node

    def explode(_usage):
        raise RuntimeError("usage broken")

    monkeypatch.setattr(model_node, "usage_from_response", explode)
    (tmp_path / "a.txt").write_text("x")
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [([{"tool_calls": [tool_call("c1", "read_file", _READ)]}], "tool_calls")],
    )
    try:
        env.send()
        await env.until("failed")
        [part] = tool_parts(env.live())
        assert part["status"] == "cancelled"
        env.assert_parity()
    finally:
        await env.close()


async def test_an_agent_message_on_the_way_stays_in_the_reply(backend_env, tmp_path, monkeypatch):
    """途中来信是当前回复的一段：不开新回合，刷新后位置也不变。"""
    (tmp_path / "a.txt").write_text("x")

    async def first_response():
        env.send_agent("上级追加")
        await asyncio.sleep(0.05)  # 让 actor 把它投进 mailbox，下一次模型请求前才会并入
        tool = tool_call("c1", "read_file", _READ)
        deltas = [{"content": "进展"}, {"tool_calls": [tool]}]
        for frame in sdk_events(deltas, finish_reason="tool_calls"):
            yield frame

    env = LiveColdEnv(tmp_path, monkeypatch, [first_response, ([{"content": "结论"}], "stop")])
    try:
        env.send()
        await env.until("completed")
        user, reply = env.live()["messages"]
        assert user["source"] is None
        assert [part["type"] for part in reply["parts"]] == [
            "text",
            "tool-call",
            "agent-message",
            "text",
        ]
        env.assert_parity()
    finally:
        await env.close()


async def test_a_queued_notice_reaches_the_model_right_before_the_input_that_starts_the_run(
    backend_env, tmp_path, monkeypatch
):
    """分叉交接靠这条顺序：通知不开 Run，随任务说明开出的 Run 写在它前面，界面看不到。"""
    env = LiveColdEnv(tmp_path, monkeypatch, [([{"content": "结论"}], "stop")])
    try:
        notice = NoticeBody(content="交接通知", notice_kind="fork_handover")
        spaces.enqueue(env.session.id, SessionMessage.of(notice))
        await asyncio.sleep(0.05)  # 通知单独到达时 actor 不开 Run
        assert env.model.requests == []
        env.send_agent("任务说明")
        await env.until("completed")
        handover, assignment = env.model.requests[0][-2:]
        assert "交接通知" in handover["content"] and "<system_notice" in handover["content"]
        assert "任务说明" in assignment["content"]
        assert "交接通知" not in str(env.live()["messages"])
        env.assert_parity()
    finally:
        await env.close()


async def test_a_cancelled_run_keeps_its_pending_decision(backend_env, tmp_path, monkeypatch):
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [([{"tool_calls": [tool_call("h1", "probe_hitl", {"intent": "批准"})]}], "tool_calls")],
    )
    try:
        env.send()
        await env.parked()
        await AssistantTransportService().cancel(env.session.id)
        await env.until("cancelled")
        [part] = tool_parts(env.live())
        assert part["awaitingUser"] is True
        env.assert_parity()
    finally:
        await env.close()


async def test_resuming_a_failed_run_keeps_live_and_cold_in_step(backend_env, tmp_path, monkeypatch):
    """恢复清空结局、续写同一回合的回复：刷新前后都只有这一条回复，可恢复判定一致。"""
    env = LiveColdEnv(
        tmp_path, monkeypatch, [RuntimeError("boom"), ([{"content": "恢复后"}], "stop")]
    )
    try:
        env.send()
        await env.until("failed")
        assert env.live()["activity"]["resumable"] is True
        env.assert_parity()

        await spaces.get(env.session.id).actor.resume()
        await env.until("completed")
        state = env.live()
        replies = [m for m in state["messages"] if m["role"] == "assistant"]
        assert [m["outcome"]["status"] for m in replies] == ["completed"]
        assert state["activity"]["resumable"] is False
        env.assert_parity()
    finally:
        await env.close()


async def test_resuming_a_cancelled_run_keeps_live_and_cold_in_step(backend_env, tmp_path, monkeypatch):
    """取消后恢复：同一个 Run 续写同一回合，结局只落在这一条回复上，可恢复判定实时与冷重建一致。"""
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [
            ([{"tool_calls": [tool_call("h1", "probe_hitl", {"intent": "批准"})]}], "tool_calls"),
            ([{"content": "恢复后"}], "stop"),
        ],
    )
    try:
        env.send()
        await env.parked()
        await AssistantTransportService().cancel(env.session.id)
        await env.until("cancelled")
        assert env.live()["activity"]["resumable"] is True
        env.assert_parity()

        await spaces.get(env.session.id).actor.resume()
        await env.until("completed")
        state = env.live()
        replies = [m for m in state["messages"] if m["role"] == "assistant"]
        assert [m["outcome"]["status"] for m in replies] == ["completed"]
        assert state["activity"]["resumable"] is False
        env.assert_parity()
    finally:
        await env.close()


async def test_inputs_merged_together_each_keep_their_reply(backend_env, tmp_path, monkeypatch):
    """挂起的 Run 被两条立即发送一起唤醒：两个回合各自有回复（前一条为空），结局落在后一条上。"""
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [
            ([{"tool_calls": [tool_call("h1", "probe_hitl", {"intent": "批准"})]}], "tool_calls"),
            ([{"content": "结论"}], "stop"),
        ],
    )
    try:
        env.send()
        assert (await env.parked()).status == "suspended"
        env.send("一", delivery="immediate")
        env.send("二", delivery="immediate")
        await env.until("completed")
        state = env.live()
        roles = [(m["role"], bool(m["parts"]), m["outcome"] is not None) for m in state["messages"]]
        assert roles[-4:] == [
            ("user", True, False),
            ("assistant", False, False),
            ("user", True, False),
            ("assistant", True, True),
        ]
        env.assert_parity()
    finally:
        await env.close()
