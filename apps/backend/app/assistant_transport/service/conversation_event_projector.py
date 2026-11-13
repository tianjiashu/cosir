"""把 workflow conversation facts 投影为 Assistant Transport snapshot。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.assistant_transport.stream import TransportFrame
from app.core.events import SessionQueueChangedEvent, WorkflowEvent
from app.service.depends import get_conversation_state_service, get_session_feed

if TYPE_CHECKING:
    from app.assistant_transport.service.conversation_state_service import (
        ConversationStateService,
    )


class ConversationEventProjector:
    """按事件顺序把 conversation event 投影为 Session snapshot：Transport 适配边界。

    只读取事件、维护 snapshot，不改 context 或 ``RuntimeMessageContextManager`` 的消息正文；mutation
    由 ``assistant_transport.projection.plan_event`` 按事件类型规划，变更与订阅者通知在 state service
    的锁内完成。事件在单个 backend 进程内按 workflow 的消费顺序处理；事件 id 每次新生成、没有重放
    路径，因此不做按 id 去重，非增量事件由各 planner 按业务键幂等（见 ``core.events`` 的契约）。
    """

    def __init__(self, state_service: ConversationStateService | None = None) -> None:
        """``state_service`` 省略时用进程级实例；测试可注入内存实现以脱离 SQLite。"""

        self._state_service = state_service or get_conversation_state_service()

    def process(self, event: WorkflowEvent) -> TransportFrame | None:
        """投影一条事件并通知订阅者；事件模型构造时已完成契约校验。

        「队列变了」不经 planner：排队投影镜像的是外部队列表，由 state service 整表重读（要读库，规划
        函数是纯的）；它失败只记录，返回 ``None``。

        异常:
            ValueError: 事件不满足 snapshot 投影规则。
            KeyError: 事件引用的消息、part 或工具调用不存在，或 Session 已删除。
        """

        if isinstance(event, SessionQueueChangedEvent):
            self._state_service.refresh_queued_messages(event.session_id)
            return None
        change = self._state_service.apply_planned(event)
        # Session 推送：执行状态随 Run 开始与迁移变化，等你操作随工具调用状态迁移变化（等待占位
        # 的写入、决定被接收、Run 外结算都经这个事件）；Run 开始还刷新了 Session 的排序时间，是目录
        # 行的变化。都在投影锁外发布（要读库）。
        if event.type in ("run_initialized", "run_status_changed", "tool_call_status_changed"):
            get_session_feed().publish_status(event.session_id)
        if event.type == "run_initialized":
            get_session_feed().publish_session_catalog(event.session_id)
        return change
