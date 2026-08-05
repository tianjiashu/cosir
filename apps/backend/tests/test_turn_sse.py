"""``turns_api._sse_turn_events`` 的流式时序测试。

聚焦一个端到端集成缺陷：真实 ``runner.run_turn`` 在产出 ``RUN_FINISHED``（经 yield
发布到 bus）之后，还会在 ``_publish_stable_file_changes`` 里直接发布
``FILE_CHANGE_STABLE`` 事件。若 ``_sse_turn_events`` 收到 ``RUN_FINISHED`` 就立即
break，后者会滞留在订阅队列无法送达前端，导致「变更面板随对话自动刷新」失效。

本测试用一个 stub runtime 模拟上述时序，验证 ``_sse_turn_events`` 能同时把
``RUN_FINISHED`` 与随后的 ``FILE_CHANGE_STABLE`` 都编码为 SSE 帧送出。
"""

import json
from collections.abc import AsyncIterator

import pytest

from app.api.turns_api import _sse_turn_events
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload.file_change_stable_payload import FileChangeStablePayload
from app.models.payload.run_finished_payload import RunFinishedPayload
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus


class _StubRuntime:
    """模拟真实 runner：RUN_FINISHED 之后直接发布 FILE_CHANGE_STABLE。"""

    def __init__(self, event_bus: RuntimeEventBus, turn_id: str) -> None:
        self._event_bus = event_bus
        self._turn_id = turn_id

    async def run_turn(self, turn_id: str, turn=None) -> AsyncIterator[RuntimeEvent]:
        """产出 RUN_FINISHED 后，模拟 runner 在收尾阶段直接广播 file_change_stable。

        参数:
            turn_id: 轮次标识。
            turn: 未使用，仅满足调用契约。

        生成:
            先 yield RUN_FINISHED，随后向 bus 发布 FILE_CHANGE_STABLE（不经 yield）。
        """
        yield RuntimeEvent(
            event_type=EventType.RUN_FINISHED,
            task_id="task1",
            turn_id=turn_id,
            payload=RunFinishedPayload(status="completed"),
        )
        # 真实 runner 的 _publish_stable_file_changes 在 RUN_FINISHED 之后直接 publish。
        self._event_bus.publish(
            RuntimeEvent(
                event_type=EventType.FILE_CHANGE_STABLE,
                task_id="task1",
                turn_id=turn_id,
                payload=FileChangeStablePayload(
                    task_id="task1", turn_id=turn_id, path="a.txt", action="modified"
                ),
            )
        )


def _collect_sse_frames(frames: list[str]) -> list[tuple[str, dict]]:
    """从 SSE 帧文本解析出 (event_type, payload) 列表。

    参数:
        frames: ``_sse_turn_events`` 产出的原始 SSE 帧字符串。

    返回:
        ``(event_type, data_dict)`` 元组列表。
    """
    parsed: list[tuple[str, dict]] = []
    for frame in frames:
        event_line = next((line for line in frame.split("\n") if line.startswith("event: ")), "")
        data_line = next((line for line in frame.split("\n") if line.startswith("data: ")), "")
        event_type = event_line[len("event: ") :].strip()
        data = json.loads(data_line[len("data: ") :])
        parsed.append((event_type, data))
    return parsed


@pytest.mark.asyncio
async def test_sse_delivers_file_change_stable_after_run_finished() -> None:
    """RUN_FINISHED 之后发布的 FILE_CHANGE_STABLE 必须能被编码为 SSE 帧送达前端。"""
    bus = RuntimeEventBus()
    runtime = _StubRuntime(bus, "turn1")

    frames: list[str] = []
    async for frame in _sse_turn_events(runtime, "turn1", None, bus):
        frames.append(frame)

    parsed = _collect_sse_frames(frames)
    event_types = [event_type for event_type, _ in parsed]
    # RUN_FINISHED 之后必须还能收到 FILE_CHANGE_STABLE，而非被 break 截断。
    assert EventType.RUN_FINISHED.value in event_types
    assert EventType.FILE_CHANGE_STABLE.value in event_types
    assert event_types.index(EventType.FILE_CHANGE_STABLE.value) > event_types.index(
        EventType.RUN_FINISHED.value
    )
    stable_data = next(data for et, data in parsed if et == EventType.FILE_CHANGE_STABLE.value)
    assert stable_data["payload"]["path"] == "a.txt"
    assert stable_data["payload"]["action"] == "modified"
