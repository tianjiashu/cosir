"""Subscribe-only turn event stream tests."""

import pytest
from fastapi import HTTPException

from app.api.turns_api import subscribe_turn_events
from app.models import TurnRecord
from app.service.agent_runtime_event.runtime_event_bus import RuntimeEventBus
from app.utils.datetime_utils import utc_now


class _TurnService:
    """Provide a minimal real turn lookup boundary for endpoint tests."""

    def __init__(self, turn: TurnRecord | None) -> None:
        """保存 ``get_turn`` 要返回的预设轮次。

        参数:
            turn: 查询成功时返回的轮次；为 ``None`` 时模拟轮次不存在。

        返回:
            无。

        异常:
            无。

        副作用:
            保存测试替身的轮次查询结果。
        """

        self._turn = turn

    def get_turn(self, turn_id: str) -> TurnRecord:
        """返回预设轮次或模拟生产环境的缺失轮次错误。

        参数:
            turn_id: 待查询轮次标识，仅用于构造缺失时的 ``KeyError``。

        返回:
            预设的轮次记录。

        异常:
            KeyError: 预设轮次为 ``None`` 时抛出。

        副作用:
            无。
        """

        if self._turn is None:
            raise KeyError(turn_id)
        return self._turn


class _ClosingEventBus(RuntimeEventBus):
    """Close the subscription immediately so the test can consume its response."""

    def subscribe(self, turn_id: str):
        """通过真实事件总线订阅后立即关闭测试流。

        参数:
            turn_id: 待订阅事件的轮次标识。

        返回:
            已被关闭、可供测试消费的事件订阅对象。

        异常:
            ValueError: ``turn_id`` 为空时由父类订阅实现抛出。

        副作用:
            注册订阅并立即向其写入关闭哨兵。
        """

        subscription = super().subscribe(turn_id)
        self.close_turn(turn_id)
        return subscription

    def claim_turn_producer(self, turn_id: str) -> bool:
        """在仅订阅端点错误认领 producer 时使测试失败。

        参数:
            turn_id: 被错误认领 producer 的轮次标识。

        返回:
            无；本方法总会抛出异常。

        异常:
            AssertionError: 仅订阅端点调用本方法时抛出。

        副作用:
            无。
        """

        raise AssertionError(f"subscribe-only endpoint claimed producer for {turn_id}")


def _turn(status: str) -> TurnRecord:
    """构造端点行为测试所需的轮次值对象。

    参数:
        status: 要设置给测试轮次的执行状态。

    返回:
        带固定父子关联字段的轮次记录。

    异常:
        无。

    副作用:
        无。
    """

    now = utc_now()
    return TurnRecord(
        turn_id="turn_child",
        task_id="task_1",
        input_text="review this",
        status=status,
        created_at=now,
        updated_at=now,
        parent_turn_id="turn_parent",
        delegation_id="del_1",
    )


async def test_subscribe_turn_events_rejects_missing_turn():
    """验证请求不存在的子轮次时端点返回 404。

    参数:
        无。

    返回:
        无。

    异常:
        无；预期 HTTPException 在测试内被捕获。

    副作用:
        调用订阅端点的缺失轮次守卫分支。
    """

    with pytest.raises(HTTPException) as exc_info:
        await subscribe_turn_events("missing", _TurnService(None), RuntimeEventBus())

    assert exc_info.value.status_code == 404


async def test_subscribe_turn_events_rejects_terminal_turn():
    """验证请求已终态子轮次时端点返回 409。

    参数:
        无。

    返回:
        无。

    异常:
        无；预期 HTTPException 在测试内被捕获。

    副作用:
        调用订阅端点的终态轮次守卫分支。
    """

    with pytest.raises(HTTPException) as exc_info:
        await subscribe_turn_events(
            "turn_child", _TurnService(_turn("completed")), RuntimeEventBus()
        )

    assert exc_info.value.status_code == 409


async def test_subscribe_turn_events_returns_sse_without_invoking_runtime():
    """验证 pending 子轮次仅订阅 SSE 而不认领 producer 或运行时。

    参数:
        无。

    返回:
        无。

    异常:
        AssertionError: 端点错误认领 producer 时由测试事件总线抛出。

    副作用:
        创建并消费一个立即关闭的 SSE 订阅响应。
    """

    response = await subscribe_turn_events(
        "turn_child", _TurnService(_turn("pending")), _ClosingEventBus()
    )

    assert response.media_type.startswith("text/event-stream")
    assert [chunk async for chunk in response.body_iterator] == []
