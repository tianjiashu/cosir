# ruff: noqa: F811 - ``derived`` 夹具从 test_derived_report 导入后作参数
"""订阅者合并帧的前提：只有一条 ``append-text`` 的帧，只写当前最后一条消息。

合并帧取后一帧的 revision、内容含前一帧（``Subscriber._coalesce``）；前端只按 revision 回放写到未加载
消息上的变更，最后一条消息总在已加载的窗口里，所以前提成立时合并不会让历史页与帧重复。这里跑一条真实
链路（多块流式正文、前台终端输出、委派与汇报、第二个用户回合），把每一帧应用到镜像上逐帧核对。
"""

import asyncio
import copy

from app.service import depends
from tests.sdk_fixtures import sdk_events
from tests.test_derived_report import (  # noqa: F401 - derived 是夹具
    ASSIGNMENT,
    _call,
    _child_id,
    _Command,
    _runs,
    _send,
    _settle,
    _until,
    derived,
)


def _chunks(text: str) -> list[dict]:
    return [{"content": word + " "} for word in text.split()]


class _StreamingModel:
    """父：先流式说几句再委派，读到汇报后再说；子：流式说话、跑一条前台命令、再说完。"""

    def __init__(self) -> None:
        self.child_gate = asyncio.Event()  # 子 Session 的订阅挂上之后才放行

    async def stream(self, messages, *, tools):
        inputs = [str(m.get("content")) for m in messages if m["role"] == "user"]
        tools_done = [m for m in messages if m["role"] == "tool"]
        child = any('from="parent"' in text and ASSIGNMENT in text for text in inputs)
        if child:
            await self.child_gate.wait()
        if child and not tools_done:
            call = _call("child-run", "execute_terminal", {"intent": "build", "command": "build"})
            frames = sdk_events(
                [*_chunks("child is building now"), {"tool_calls": [call]}],
                finish_reason="tool_calls",
            )
        elif child:
            frames = sdk_events(_chunks("child finished the build"))
        elif not tools_done:
            call = _call(
                "call-delegate",
                "delegate_task",
                {
                    "child_agent_id": "general-purpose",
                    "agent_name": "review",
                    "message": ASSIGNMENT,
                },
            )
            frames = sdk_events(
                [*_chunks("parent will delegate this"), {"tool_calls": [call]}],
                finish_reason="tool_calls",
            )
        else:
            frames = sdk_events(_chunks(f"parent read {len(inputs)} inputs and answers"))
        for frame in frames:
            await asyncio.sleep(0.01)  # 留出间隔：一口气到达的块会被正文聚合成一次写入
            yield frame

    async def close(self):
        pass


async def _record(
    session_id: int, violations: list[str], frames: list[int], appends: dict[int, int]
) -> None:
    states = depends.get_conversation_state_service()
    subscriber, unsubscribe, first = states.subscribe_with_snapshot(session_id)
    mirror = copy.deepcopy(first.state)
    try:
        while True:
            frame = await subscriber.get()
            if frame.kind != "mutation":
                assert frame.kind == "full", frame.resync_reason
                mirror = copy.deepcopy(frame.state)
                continue
            frames.append(len(frame.mutations))
            if len(frame.mutations) == 1 and frame.mutations[0].kind == "append-text":
                appends[session_id] = appends.get(session_id, 0) + 1
                path = frame.mutations[0].path
                last = len(mirror["messages"]) - 1
                if path[0] != "messages" or path[1] != last:
                    violations.append(f"{path} while last message is {last}")
            for mutation in frame.mutations:
                mutation.apply_to(mirror)
    finally:
        unsubscribe()


def _completed_runs(session_id: int) -> int:
    return sum(run.status == "completed" for run in _runs(session_id))


async def test_coalescible_frames_only_target_the_active_reply(derived):
    _Command.gate.set()
    derived.model = _StreamingModel()
    violations: list[str] = []
    parent_frames: list[int] = []
    child_frames: list[int] = []
    appends: dict[int, int] = {}
    recorders = [
        asyncio.create_task(_record(derived.parent.id, violations, parent_frames, appends))
    ]
    try:
        await asyncio.sleep(0)
        _send(derived.parent.id, derived.model_config.id, "start")
        await _until(
            derived.parent.id,
            lambda: depends.get_session_service().list_derived_sessions(derived.parent.id),
        )
        child_id = _child_id(derived.parent.id)
        recorders.append(asyncio.create_task(_record(child_id, violations, child_frames, appends)))
        await asyncio.sleep(0.05)
        derived.model.child_gate.set()
        await _until(
            derived.parent.id,
            lambda: len(_runs(derived.parent.id)) == 2
            and _runs(derived.parent.id)[-1].status == "completed",
        )
        _send(derived.parent.id, derived.model_config.id, "again")
        await _until(
            derived.parent.id,
            lambda: len(_runs(derived.parent.id)) == 3
            and _runs(derived.parent.id)[-1].status == "completed",
        )
        await _settle(child_id, derived.parent.id)
        await asyncio.sleep(0.1)
    finally:
        for recorder in recorders:
            recorder.cancel()
        await asyncio.gather(*recorders, return_exceptions=True)

    assert violations == []
    # 链路确实产出了可合并的帧，也产出了多变更帧（终端输出、新回合），断言不是空转。
    assert appends[derived.parent.id] >= 2 and appends[child_id] >= 1
    assert any(count > 1 for count in parent_frames)
    assert child_frames and any(count > 1 for count in child_frames)
