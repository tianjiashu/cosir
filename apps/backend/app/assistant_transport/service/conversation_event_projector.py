"""把 workflow conversation facts 投影为 Assistant Transport snapshot。"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from threading import RLock
from typing import Any, Literal, Protocol

from pydantic import TypeAdapter, ValidationError

from app.assistant_transport.service.conversation_task_snapshot_service import (
    SnapshotChange,
)
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.workflows.event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    ContextUsageUpdatedEvent,
    ConversationEvent,
    ConversationEventEnvelope,
    RunInitializedEvent,
    RunStatusChangedEvent,
    ToolCallCreatedEvent,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
    UsageUpdatedEvent,
    UserInputAppendedEvent,
)
from app.service.depends import get_conversation_task_snapshot_service


class SnapshotOwnerPort(Protocol):
    """Projector 所需的 snapshot 读写端口。

    生产 ``ConversationTaskSnapshotService`` 与测试内存实现均按结构满足本端口，
    projector 因此无需依赖具体 owner，可在无 SQLite 的环境下被驱动。
    """

    def ensure_state_snapshot(self, task_id: int) -> ConversationStateSnapshot:
        """读取（必要时初始化）指定 Task 的 snapshot 副本。"""
        ...

    def apply_planned(
        self,
        task_id: int,
        planner: Callable[[ConversationStateSnapshot], Sequence[ConversationStateMutation]],
    ) -> SnapshotChange:
        """在 owner 锁内规划并提交一次 mutation 批次。"""
        ...


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
    "usage_updated",
    "context_usage_updated",
}


class ConversationEventProjector:
    """按事件顺序把 conversation event 更新到 Task snapshot。

    Projector 是 Transport 适配边界：它只读取 event 并维护 snapshot，不触碰
    ``RuntimeContextManager``。事件在单个 backend 进程内按 workflow 的消费顺序处理；
    ``event_id`` 用于抵御同一事件的重复投递，尤其是不可重复追加的文本 delta。
    """

    def __init__(self, snapshot_service: SnapshotOwnerPort | None = None) -> None:
        """初始化 snapshot 投影器。

        参数:
            snapshot_service: 承载 snapshot 读写的 owner；省略时使用进程级默认实例，
                测试可注入内存实现以脱离 SQLite 运行。

        返回:
            无。

        异常:
            RuntimeError: 省略 ``snapshot_service`` 且主库存储尚未初始化。

        副作用:
            无；projector 的去重集合仅存在于当前 backend 进程内。
        """
        self._snapshot_service = snapshot_service or get_conversation_task_snapshot_service()
        self._lock = RLock()
        self._seen_event_ids: dict[int, set[str]] = {}

    def process(self, raw_event: object) -> SnapshotChange | None:
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
            经 snapshot owner 持久化 snapshot，并通知 Transport 订阅者。
        """

        event = self._parse(raw_event)
        if event is None:
            return None
        with (self._lock):
            seen = self._seen_event_ids.setdefault(event.task_id, set())
            if event.event_id in seen:
                state = self._snapshot_service.ensure_state_snapshot(event.task_id)
                return SnapshotChange(event.task_id, state, ())

            change = self._snapshot_service.apply_planned(
                event.task_id,
                lambda state: self._plan(state, event),
            )
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

    def _plan(
        self,
        state: ConversationStateSnapshot,
        event: ConversationEvent,
    ) -> Sequence[ConversationStateMutation]:
        """为一条已校验事件生成 snapshot mutations。"""

        if isinstance(event, RunInitializedEvent):
            return self._plan_run_initialized(state, event)
        if isinstance(event, UserInputAppendedEvent):
            return self._plan_user_input(state, event)
        if isinstance(event, AssistantTextDeltaEvent):
            return self._plan_text_delta(state, event)
        if isinstance(event, AssistantPartClosedEvent):
            return self._plan_part_closed(state, event)
        if isinstance(event, ToolCallCreatedEvent):
            return self._plan_tool_created(state, event)
        if isinstance(event, ToolCallStatusChangedEvent):
            return self._plan_tool_status(state, event)
        if isinstance(event, ToolCallsSettledEvent):
            return self._plan_tools_settled(state, event)
        if isinstance(event, RunStatusChangedEvent):
            return self._plan_run_status(state, event)
        if isinstance(event, UsageUpdatedEvent):
            return self._plan_usage(event)
        if isinstance(event, ContextUsageUpdatedEvent):
            return [ConversationStateMutation("set", ("context_usage",), event.ratio)]
        raise TypeError(f"unhandled conversation event: {type(event).__name__}")

    @staticmethod
    def _plan_run_initialized(
        state: ConversationStateSnapshot,
        event: RunInitializedEvent,
    ) -> list[ConversationStateMutation]:
        """规划 Run 的 user/assistant 消息骨架。"""

        if any(message.get("runId") == event.run_id for message in state["messages"]):
            return []
        offset = len(state["messages"])
        return [
            ConversationStateMutation(
                "set",
                ("messages", offset),
                _message(f"user-{event.run_id}", event.run_id, "user", "completed", "completed"),
            ),
            ConversationStateMutation(
                "set",
                ("messages", offset + 1),
                _message(
                    f"assistant-{event.run_id}", event.run_id, "assistant", "running", "running"
                ),
            ),
            ConversationStateMutation("set", ("run", "runId"), event.run_id),
            ConversationStateMutation("set", ("run", "status"), "pending"),
        ]

    @staticmethod
    def _plan_user_input(
        state: ConversationStateSnapshot,
        event: UserInputAppendedEvent,
    ) -> list[ConversationStateMutation]:
        """规划 user text part 的增量追加。"""

        message_index, part_index = _find_message_part(state, event.run_id, "user", "text")
        return [
            ConversationStateMutation(
                "append-text", ("messages", message_index, "parts", part_index, "text"), event.text
            )
        ]

    @staticmethod
    def _plan_text_delta(
        state: ConversationStateSnapshot,
        event: AssistantTextDeltaEvent,
    ) -> list[ConversationStateMutation]:
        """规划 assistant text/reasoning part 的增量追加或首次创建。"""

        message_index = _find_assistant_message(state, event.run_id)
        assert message_index is not None
        parts = state["messages"][message_index]["parts"]
        for part_index in range(len(parts) - 1, -1, -1):
            part = parts[part_index]
            if (
                isinstance(part, dict)
                and part.get("type") == event.part
                and part.get("status") == "running"
            ):
                return [
                    ConversationStateMutation(
                        "append-text",
                        ("messages", message_index, "parts", part_index, "text"),
                        event.delta,
                    )
                ]
        return [
            ConversationStateMutation(
                "set",
                ("messages", message_index, "parts", len(parts)),
                {"type": event.part, "text": event.delta, "status": "running"},
            )
        ]

    @staticmethod
    def _plan_part_closed(
        state: ConversationStateSnapshot,
        event: AssistantPartClosedEvent,
    ) -> list[ConversationStateMutation]:
        """规划 assistant 当前 part 的收口。"""

        message_index = _find_assistant_message(state, event.run_id)
        assert message_index is not None
        parts = state["messages"][message_index]["parts"]
        for part_index in range(len(parts) - 1, -1, -1):
            part = parts[part_index]
            if (
                isinstance(part, dict)
                and part.get("type") == event.part
                and part.get("status") == "running"
            ):
                return [
                    ConversationStateMutation(
                        "set",
                        ("messages", message_index, "parts", part_index, "status"),
                        "completed",
                    )
                ]
        return []

    @staticmethod
    def _plan_tool_created(
        state: ConversationStateSnapshot,
        event: ToolCallCreatedEvent,
    ) -> list[ConversationStateMutation]:
        """规划 tool-call part 的建立。"""

        message_index = _find_assistant_message(state, event.run_id)
        assert message_index is not None
        parts = state["messages"][message_index]["parts"]
        if any(
            isinstance(part, dict) and part.get("toolCallId") == event.tool_call_id
            for part in parts
        ):
            return []
        return [
            ConversationStateMutation(
                "set",
                ("messages", message_index, "parts", len(parts)),
                {
                    "type": "tool-call",
                    "toolCallId": event.tool_call_id,
                    "toolName": event.tool_name,
                    "status": "pending",
                    "args": copy.deepcopy(event.args),
                    "result": None,
                    "error": None,
                    "isError": False,
                    "approvalRequestId": None,
                },
            )
        ]

    @staticmethod
    def _plan_tool_status(
        state: ConversationStateSnapshot,
        event: ToolCallStatusChangedEvent,
    ) -> list[ConversationStateMutation]:
        """规划工具调用状态及结果的一致迁移。"""

        message_index, part_index = _find_tool(state, event.tool_call_id)
        part = state["messages"][message_index]["parts"][part_index]
        current = str(part["status"])
        allowed = {
            "pending": {"pending", "running", "completed", "failed", "cancelled"},
            "running": {"running", "completed", "failed", "cancelled"},
            "completed": {"completed"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
        }
        if event.status not in allowed[current]:
            raise ValueError(f"invalid tool transition {current} -> {event.status}")
        base = ("messages", message_index, "parts", part_index)
        return [
            ConversationStateMutation("set", (*base, "status"), event.status),
            ConversationStateMutation(
                "set", (*base, "result"), event.result if event.status == "completed" else None
            ),
            ConversationStateMutation(
                "set",
                (*base, "error"),
                event.error if event.status in {"failed", "cancelled"} else None,
            ),
            ConversationStateMutation("set", (*base, "isError"), event.status == "failed"),
        ]

    @staticmethod
    def _plan_tools_settled(
        state: ConversationStateSnapshot,
        event: ToolCallsSettledEvent,
    ) -> list[ConversationStateMutation]:
        """规划指定 Run 下所有未决工具调用的补偿性收束。"""

        mutations: list[ConversationStateMutation] = []
        for message_index, message in enumerate(state["messages"]):
            if message.get("runId") != event.run_id:
                continue
            for part_index, part in enumerate(message["parts"]):
                if (
                    not isinstance(part, dict)
                    or part.get("type") != "tool-call"
                    or part.get("status") not in {"pending", "running"}
                ):
                    continue
                base = ("messages", message_index, "parts", part_index)
                mutations.extend(
                    [
                        ConversationStateMutation("set", (*base, "status"), event.status),
                        ConversationStateMutation("set", (*base, "result"), None),
                        ConversationStateMutation("set", (*base, "error"), event.reason),
                        ConversationStateMutation(
                            "set", (*base, "isError"), event.status == "failed"
                        ),
                    ]
                )
        return mutations

    @staticmethod
    def _plan_run_status(
        state: ConversationStateSnapshot,
        event: RunStatusChangedEvent,
    ) -> list[ConversationStateMutation]:
        """规划 Run 与 assistant 消息的状态迁移。"""

        mutations = [
            ConversationStateMutation("set", ("run", "runId"), event.run_id),
            ConversationStateMutation("set", ("run", "status"), event.status.value),
        ]
        if event.usage_stats is not None:
            mutations.append(
                ConversationStateMutation("set", ("usage",), event.usage_stats.to_dict())
            )
        message_index = _find_assistant_message(state, event.run_id, required=False)
        if message_index is None:
            return mutations
        base = ("messages", message_index)
        mutations.extend(
            [
                ConversationStateMutation("set", (*base, "status"), event.status.value),
                ConversationStateMutation("set", (*base, "endReason"), event.end_reason),
            ]
        )
        if event.status.value in {"completed", "failed", "cancelled"}:
            for part_index, part in enumerate(state["messages"][message_index]["parts"]):
                if (
                    isinstance(part, dict)
                    and part.get("type") in {"text", "reasoning"}
                    and part.get("status") == "running"
                ):
                    mutations.append(
                        ConversationStateMutation(
                            "set", (*base, "parts", part_index, "status"), "completed"
                        )
                    )
        return mutations

    @staticmethod
    def _plan_usage(event: UsageUpdatedEvent) -> list[ConversationStateMutation]:
        """规划累计模型用量的完整替换。"""

        return [
            ConversationStateMutation(
                "set",
                ("usage",),
                {
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                    "total_tokens": event.total_tokens,
                    "cache_hit_tokens": event.cache_hit_tokens,
                    "cache_miss_tokens": event.cache_miss_tokens,
                    "reasoning_tokens": event.reasoning_tokens,
                },
            )
        ]

    @property
    def snapshot_service(self) -> Any:
        """返回本投影器使用的 snapshot owner，供重复事件构造无 mutation change。"""

        return self._snapshot_service


def _message(
    message_id: str,
    run_id: int,
    role: Literal["user", "assistant"],
    status: str,
    part_status: Literal["running", "completed"],
) -> ConversationStateMessage:
    """构造 Transport user/assistant 消息骨架。"""

    return {
        "id": message_id,
        "runId": run_id,
        "role": role,
        "status": status,
        "endReason": None,
        "parts": [{"type": "text", "text": "", "status": part_status}],
    }


def _find_assistant_message(
    state: ConversationStateSnapshot,
    run_id: int,
    *,
    required: bool = True,
) -> int | None:
    """按 run 找到 assistant message。"""

    for index, message in enumerate(state["messages"]):
        if message.get("runId") == run_id and message.get("role") == "assistant":
            return index
    if required:
        raise KeyError(f"assistant message for run {run_id} not found")
    return None


def _find_message_part(
    state: ConversationStateSnapshot,
    run_id: int,
    role: str,
    part_type: str,
) -> tuple[int, int]:
    """按 run、role 与 part 类型定位消息 part。"""

    for message_index, message in enumerate(state["messages"]):
        if message.get("runId") != run_id or message.get("role") != role:
            continue
        for part_index, part in enumerate(message["parts"]):
            if isinstance(part, dict) and part.get("type") == part_type:
                return message_index, part_index
    raise KeyError(f"{role} {part_type} part for run {run_id} not found")


def _find_tool(state: ConversationStateSnapshot, tool_call_id: str) -> tuple[int, int]:
    """按 Task 内唯一 toolCallId 定位 tool part。"""

    for message_index, message in enumerate(state["messages"]):
        for part_index, part in enumerate(message["parts"]):
            if (
                isinstance(part, dict)
                and part.get("type") == "tool-call"
                and part.get("toolCallId") == tool_call_id
            ):
                return message_index, part_index
    raise KeyError(tool_call_id)
