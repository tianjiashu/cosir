"""把 workflow conversation facts 投影为 Assistant Transport snapshot。"""

from __future__ import annotations

from collections import deque
from threading import RLock
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.assistant_transport.event import (
    ConversationEvent,
    ConversationEventEnvelope,
)
from app.assistant_transport.stream import TransportFrame
from app.config.constant import Constant
from app.config.logging.logger import log
from app.service.depends import get_conversation_task_state_service

_EVENT_ADAPTER: TypeAdapter[ConversationEvent] = TypeAdapter(ConversationEvent)
_KNOWN_EVENT_TYPES = {
    "run_initialized",
    "user_input_appended",
    "run_status_changed",
    "assistant_text_delta",
    "assistant_part_closed",
    "tool_call_created",
    "tool_call_status_changed",
    "tool_calls_settled",
    "context_usage_updated",
    "tool_call_runtime_update",
}


class _EventIdWindow:
    """按到达顺序保留最近 N 条 event_id 的**有界**集合。

    只服务于「同一投递路径的近距离重放」（例如 LangGraph 在 interrupt/resume 后重放
    custom stream 里缓冲的同一事件对象）。超出容量的旧 id 被淘汰后不再去重，这是刻意的
    取舍：以此换取恒定内存上界，而真实重放距离远小于窗口容量。
    """

    def __init__(self, limit: int) -> None:
        """以 ``limit`` 为容量上界构造空窗口。

        参数:
            limit: 保留的 event_id 最大条数。

        返回:
            无。

        异常:
            ValueError: ``limit`` 小于 1。

        副作用:
            创建内部的顺序队列与集合成员容器。
        """

        if limit < 1:
            raise ValueError("event dedup window limit must be positive")
        self._limit = limit
        self._order: deque[str] = deque()
        self._members: set[str] = set()

    def __contains__(self, event_id: str) -> bool:
        """返回 ``event_id`` 是否仍在窗口内。"""

        return event_id in self._members

    @property
    def limit(self) -> int:
        """返回窗口容量上界。"""

        return self._limit

    @property
    def size(self) -> int:
        """返回窗口当前保留的 event_id 条数。"""

        return len(self._order)

    def add(self, event_id: str) -> bool:
        """记录一条 event_id，并在超出容量时淘汰最旧的一条。

        参数:
            event_id: 已成功投影的事件标识；已在窗口内时不重复记录。

        返回:
            本次是否发生了淘汰；调用方据此记录容量预警日志。

        副作用:
            修改窗口内部顺序与集合成员。
        """

        if event_id in self._members:
            return False
        self._order.append(event_id)
        self._members.add(event_id)
        evicted = False
        while len(self._order) > self._limit:
            self._members.discard(self._order.popleft())
            evicted = True
        return evicted


class ConversationEventProjector:
    """按事件顺序把 conversation event 投影为 Task snapshot。

    Projector 是 Transport 适配边界：它只读取 event 并维护 snapshot，不触碰
    ``RuntimeContextManager``。每个 event 自带 ``plan`` 方法（继承自
    ``ConversationEventEnvelope`` 的抽象契约），projector 直接调用 ``event.plan(state)``
    获得 mutation，无需按类型分派。事件在单个 backend 进程内按 workflow 的消费顺序处理。

    去重与内存边界：所有事件统一按 ``event_id`` 去重（重复事件返回 ``None``，不再重复投影）。
    窗口是进程内**有界** FIFO（容量见 ``Constant.Transport.EVENT_DEDUP_WINDOW``），且不按
    task 分桶，因此既不会被长会话的事件数撑大，也不需要任何按 task 的生命周期清理钩子。
    窗口淘汰会让极旧事件的重复投递漏网，此时依赖各事件 ``plan`` 的自幂等性兜底；
    ``RunInitializedEvent(replace_existing=True)`` 是唯一的例外（它整体 ``set`` 会重置 run）。
    """

    def __init__(self, state_service: Any | None = None) -> None:
        """初始化 snapshot 投影器。

        参数:
            state_service: 承载进程内 state 的 owner；省略时使用进程级默认实例，
                测试可注入内存实现以脱离 SQLite 运行。

        返回:
            无。

        异常:
            RuntimeError: 省略 ``state_service`` 且主库存储尚未初始化。

        副作用:
            无；projector 的去重窗口仅存在于当前 backend 进程内，且容量恒定。
        """

        self._state_service = state_service or get_conversation_task_state_service()
        self._lock = RLock()
        self._dedup_window = _EventIdWindow(Constant.Transport.EVENT_DEDUP_WINDOW)

    def process(
        self,
        raw_event: object,
    ) -> TransportFrame | None:
        """校验并投影一条事件。

        参数:
            raw_event: workflow custom stream 产出的 event 对象或其 JSON 字典。
        返回:
            已提交的 ``TransportFrame``；未知或重复事件返回 ``None``。未知事件只记 warning，
            已知但格式非法的事件抛出 ``ValueError``。

        异常:
            ValueError: 已知事件无法通过判别式契约校验，或事件不满足 snapshot 投影规则。
            KeyError: 事件引用的消息、part 或工具调用不存在。

        副作用:
            只更新进程内 Transport state 并通知订阅者；不写入数据库。
        """

        event = self._parse(raw_event)
        if event is None:
            return None
        with self._lock:
            if event.event_id in self._dedup_window:
                log.debug(
                    "conversation_event_duplicate_dropped",
                    extra={
                        "msg": "重复投递的事件已丢弃",
                        "data": {
                            "task_id": event.task_id,
                            "run_id": event.run_id,
                            "event_id": event.event_id,
                            "event_type": event.type,
                        },
                    },
                )
                return None
            change = self._state_service.apply_planned(event)
            if event.type == "run_status_changed":
                try:
                    self._state_service.refresh_parent_delegation(
                        event.task_id,
                        event.run_id,
                        event.status.value,
                        event.end_reason,
                    )
                except AttributeError:
                    # In-memory projector test owners intentionally implement only the
                    # snapshot mutation protocol; the production state service owns the
                    # parent delegation side projection.
                    log.debug(
                        "conversation_parent_delegation_refresh_unavailable",
                        extra={
                            "msg": "snapshot owner未装配父委派刷新能力",
                            "data": {"task_id": event.task_id, "run_id": event.run_id},
                        },
                    )
            # 事件可能先于 run 骨架抵达；空投影不能被永久去重，否则后续无法重放。
            if change.mutations and self._dedup_window.add(event.event_id):
                log.warning(
                    "conversation_event_dedup_window_evicted",
                    extra={
                        "msg": "事件去重窗口已满，已淘汰最旧 event_id",
                        "data": {
                            "task_id": event.task_id,
                            "event_id": event.event_id,
                            "window_limit": self._dedup_window.limit,
                        },
                    },
                )
            return change

    @staticmethod
    def _parse(raw_event: object) -> ConversationEvent | None:
        """把 workflow 输出解析成已知的判别式 event。"""

        if isinstance(raw_event, ConversationEventEnvelope):
            raw_event = raw_event.model_dump()
        if not isinstance(raw_event, dict):
            log.warning(
                "conversation_event_ignored",
                extra={
                    "msg": "忽略非对象 workflow event",
                    "data": {"event_type": type(raw_event).__name__},
                },
            )
            return None
        if raw_event.get("type") not in _KNOWN_EVENT_TYPES:
            log.warning(
                "conversation_event_unknown_type",
                extra={
                    "msg": "忽略未知 conversation event 类型",
                    "data": {"type": raw_event.get("type")},
                },
            )
            return None
        try:
            return _EVENT_ADAPTER.validate_python(raw_event)
        except ValidationError as exc:
            raise ValueError("invalid conversation event") from exc

    @property
    def state_service(self) -> Any:
        """返回本投影器使用的进程内 state owner。"""

        return self._state_service
