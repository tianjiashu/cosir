"""把 workflow conversation facts 投影为 Assistant Transport snapshot。"""

from __future__ import annotations

from collections.abc import Sequence
from threading import RLock
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.assistant_transport.event import (
    ConversationEvent,
    ConversationEventEnvelope,
)
from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)
from app.assistant_transport.stream import SnapshotChange
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
}


class ConversationEventProjector:
    """按事件顺序把 conversation event 投影为 Task snapshot。

    Projector 是 Transport 适配边界：它只读取 event 并维护 snapshot，不触碰
    ``RuntimeContextManager``。每个 event 自带 ``plan`` 方法（继承自
    ``ConversationEventEnvelope`` 的抽象契约），projector 直接调用 ``event.plan(state)``
    获得 mutation，无需按类型分派。事件在单个 backend 进程内按 workflow 的消费顺序处理；
    ``event_id`` 用于抵御同一事件的重复投递，尤其是不可重复追加的文本 delta。
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
            无；projector 的去重集合仅存在于当前 backend 进程内。
        """

        self._state_service = state_service or get_conversation_task_state_service()
        self._lock = RLock()
        self._seen_event_ids: dict[int, set[str]] = {}
        self._deleted_task_ids: set[int] = set()

    def mark_task_deleted(self, task_id: int) -> None:
        """标记 Task 已删除并丢弃其进程内事件去重状态。"""

        with self._lock:
            self._deleted_task_ids.add(task_id)
            self._seen_event_ids.pop(task_id, None)

    def process(
        self,
        raw_event: object,
    ) -> SnapshotChange | None:
        """校验并投影一条事件。

        参数:
            raw_event: workflow custom stream 产出的 event 对象或其 JSON 字典。
        返回:
            已提交的 ``SnapshotChange``；未知事件返回 ``None``；重复事件返回无 mutation
            的 change。未知事件只记 warning，已知但格式非法的事件抛出 ``ValueError``。

        异常:
            ValueError: 已知事件无法通过判别式契约校验，或事件不满足 snapshot 投影规则。
            KeyError: 事件引用的消息、part 或工具调用不存在。

        副作用:
            只更新进程内 Transport state 并通知订阅者；不写入数据库。
        """

        event = self._parse(raw_event)
        if event is None:
            return None
        generation_check = getattr(self._state_service, "is_current_generation", None)
        if generation_check is not None and not generation_check():
            log.info(
                "conversation_event_stale_generation_ignored",
                extra={
                    "msg": "忽略旧 backend generation 的 conversation event",
                    "data": {"task_id": event.task_id, "event_id": event.event_id},
                },
            )
            return None
        with self._lock:
            if event.task_id in self._deleted_task_ids:
                log.info(
                    "conversation_event_ignored_for_deleted_task",
                    extra={
                        "msg": "忽略已删除 task 的迟到 conversation event",
                        "data": {"task_id": event.task_id, "event_id": event.event_id},
                    },
                )
                return None
            seen = self._seen_event_ids.setdefault(event.task_id, set())
            if event.event_id in seen:
                state = self._state_service.get_state(event.task_id)
                return SnapshotChange(event.task_id, state, ())

            def planner(state: ConversationStateSnapshot) -> Sequence[ConversationStateMutation]:
                if event.type not in {"run_initialized", "context_usage_updated"} and (
                    event.run_id is None
                    or not any(run["runId"] == event.run_id for run in state["runs"])
                ):
                    log.warning(
                        "conversation_event_unknown_run",
                        extra={
                            "msg": "忽略引用未知 Run 的 conversation event",
                            "data": {
                                "task_id": event.task_id,
                                "run_id": event.run_id,
                                "event_id": event.event_id,
                                "event_type": event.type,
                            },
                        },
                    )
                    return []
                return event.plan(state)

            change = self._state_service.apply_planned(event.task_id, planner)
            # 事件可能先于 run 骨架抵达；空投影不能被永久去重，否则后续无法重放。
            if change.mutations:
                seen.add(event.event_id)
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
