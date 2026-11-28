"""事件是旁路：Run 迁移提交之后投影抛出，不能让 Run 停在 running 却没有执行者。

真实 actor、RunLedger、projector、workflow 与隔离 SQLite；只把模型换成脚本替身。投影失败按 projector
自己声明的契约注入（``ValueError``：事件不满足投影规则），只在指定的那一个事件上抛一次。
"""

import asyncio
import json

import pytest

from app.assistant_transport.service.assistant_transport_service import AssistantTransportService
from app.assistant_transport.service.conversation_event_projector import ConversationEventProjector
from app.service import depends
from tests.live_cold_support import LiveColdEnv

# 每个用例都有意让投影失败一次（对照用例不注入，守卫照常生效也无妨）。
pytestmark = pytest.mark.event_sink_failure_expected


def _hitl_call() -> dict:
    return {
        "index": 0,
        "id": "call-ask",
        "type": "function",
        "function": {"name": "probe_hitl", "arguments": json.dumps({})},
    }


def _fail_once(monkeypatch, matches) -> list[str]:
    """让 projector 在第一个满足 ``matches`` 的事件上抛出；返回命中的事件类型。"""
    original = ConversationEventProjector.process
    hits: list[str] = []

    def process(self, event):
        if not hits and matches(event):
            hits.append(event.type)
            raise ValueError("projection rule violated")
        return original(self, event)

    monkeypatch.setattr(ConversationEventProjector, "process", process)
    return hits


def _runs(env: LiveColdEnv):
    return depends.get_run_crud().list_by_session(env.session.id)


async def test_run_runs_when_its_initialization_projection_fails(backend_env, tmp_path, monkeypatch):
    env = LiveColdEnv(tmp_path, monkeypatch, [([{"content": "done"}], "stop")])
    hits = _fail_once(monkeypatch, lambda event: event.type == "run_initialized")
    try:
        env.send("start")
        run = await env.parked()
        assert hits == ["run_initialized"]
        assert run.status == "completed" and len(env.model.requests) == 1
    finally:
        await env.close()


async def test_woken_run_runs_when_its_status_projection_fails(backend_env, tmp_path, monkeypatch):
    env = LiveColdEnv(
        tmp_path,
        monkeypatch,
        [([{"tool_calls": [_hitl_call()]}], "tool_calls"), ([{"content": "done"}], "stop")],
    )
    try:
        env.send("start")
        await env.until("suspended")
        hits = _fail_once(
            monkeypatch,
            lambda event: event.type == "run_status_changed" and event.status.value == "running",
        )
        env.send("never mind")
        await env.until("completed")
        assert hits == ["run_status_changed"]
        assert len(_runs(env)) == 1 and len(env.model.requests) == 2
    finally:
        await env.close()


async def test_resumed_run_runs_when_its_status_projection_fails(backend_env, tmp_path, monkeypatch):
    env = LiveColdEnv(
        tmp_path, monkeypatch, [RuntimeError("model down"), ([{"content": "done"}], "stop")]
    )
    try:
        env.send("start")
        assert (await env.parked()).status == "failed"
        hits = _fail_once(
            monkeypatch,
            lambda event: event.type == "run_status_changed" and event.status.value == "running",
        )
        await AssistantTransportService().resume(env.session.id)
        await env.until("completed")
        assert hits == ["run_status_changed"]
        assert len(_runs(env)) == 1 and len(env.model.requests) == 2
    finally:
        await env.close()


async def test_control_without_projection_failure(backend_env, tmp_path, monkeypatch):
    env = LiveColdEnv(tmp_path, monkeypatch, [([{"content": "done"}], "stop")])
    try:
        env.send("start")
        assert (await env.parked()).status == "completed"
        assert len(env.model.requests) == 1
    finally:
        await env.close()


async def _drain(subscriber) -> list:
    frames = []
    while True:
        try:
            frames.append(await asyncio.wait_for(subscriber.get(), 0.3))
        except TimeoutError:
            return frames


async def test_clients_converge_after_a_planning_failure(backend_env, tmp_path, monkeypatch):
    """投影在规划阶段失败（事件不满足投影规则）：Run 照常执行，订阅中的客户端也必须最终收敛到 canonical——
    要么之后收到的帧把它带回一致，要么收到要求重新同步的帧。工作副本本身也不能带着缺口继续。"""
    import copy

    from app.assistant_transport.service import conversation_state_service as state_module
    from tests.live_cold_support import normalized

    env = LiveColdEnv(tmp_path, monkeypatch, [([{"content": "done"}], "stop")])
    subscriber, unsubscribe, first = env.state.subscribe_with_snapshot(env.session.id)
    plan = state_module.plan_event
    hits: list[str] = []

    def failing_plan(event, state, catalog):
        if not hits and event.type == "run_initialized":
            hits.append(event.type)
            raise KeyError("planned against a stale working copy")
        return plan(event, state, catalog)

    monkeypatch.setattr(state_module, "plan_event", failing_plan)
    try:
        env.send("start")
        assert (await env.parked()).status == "completed"
        frames = await _drain(subscriber)
        assert hits == ["run_initialized"]

        client = copy.deepcopy(first.state)
        resynced = False
        for frame in frames:
            if frame.kind == "full":
                client = copy.deepcopy(frame.state)
            elif frame.kind == "resync_required":
                resynced = True
            else:
                for mutation in frame.mutations:
                    mutation.apply_to(client)
        cold = normalized(env.cold())
        assert resynced or normalized(client) == cold
        assert normalized(env.live()) == cold
    finally:
        unsubscribe()
        await env.close()


async def test_a_projection_failure_mid_stream_loses_no_text(backend_env, tmp_path, monkeypatch):
    """流式正文中途投影失败：工作副本被丢弃、按落库的草稿重建，之后的增量接着投影。

    这是 ``_discard`` 接受的代价（见 ``ConversationStateService._unload_if_unwatched``）：文字不丢，但重建把
    草稿当成已完成的段，后续增量另起一段，触发重建的那一块重复一次；冷重建（刷新、卸载后）恢复成一段。
    """
    from tests.sdk_fixtures import sdk_events

    words = [f"w{index} " for index in range(12)]

    async def slow_stream():
        for frame in sdk_events([{"content": word} for word in words]):
            await asyncio.sleep(0.05)  # 留出间隔：一口气到达的块会被正文聚合成一次写入
            yield frame

    env = LiveColdEnv(tmp_path, monkeypatch, [slow_stream])
    from app.assistant_transport.service import conversation_state_service as state_module

    plan = state_module.plan_event
    deltas: list[int] = []
    hits: list[str] = []

    def failing_plan(event, state, catalog):
        # 规划在工作副本上失败（与 ``test_clients_converge_after_a_planning_failure`` 同一注入点），
        # 落在第二块正文上：此时草稿行已落库、工作副本里已有第一块。
        if event.type == "assistant_text_delta":
            deltas.append(1)
            if len(deltas) == 2:
                hits.append(event.type)
                raise KeyError("planned against a stale working copy")
        return plan(event, state, catalog)

    monkeypatch.setattr(state_module, "plan_event", failing_plan)
    try:
        env.send("start")
        assert (await env.parked()).status == "completed"
        assert hits == ["assistant_text_delta"]

        def texts(state) -> list[str]:
            reply = state["messages"][-1]
            return [part["text"] for part in reply["parts"] if part["type"] == "text"]

        assert texts(env.cold()) == ["".join(words)]
        live = texts(env.live())
        assert len(live) == 2
        tokens = "".join(live).split()
        assert len(tokens) == len(words) + 1  # 只多出被重复的那一块
        assert list(dict.fromkeys(tokens)) == [word.strip() for word in words]
    finally:
        await env.close()
