"""执行事件的消费回调：core 交出事件的唯一出口（依赖倒置，core 不导入 ``assistant_transport``）。

执行内核与调度只认这个回调，不知道事件被投影到哪里：对话场景由 lifespan 装配时注入 Assistant
Transport projector 的 ``process``（测试由 conftest 注入）。未注入就发事件属于装配缺陷，直接报错，
不静默丢弃。

事件是旁路：交出事件时事实已经提交，消费失败不能改变执行。这条规则只在这里执行一次——注入时包一层，
消费方抛出的异常记录后吞掉，调用方（Run 台账的迁移、workflow 节点、工具的实时输出、Run 外的占位结算）
不再各自捕获。否则提交与登记执行之间的一次投影异常，就会留下「running 但没有执行者」的 Run。
消费方自己负责收敛：投影失败丢弃工作副本、要求订阅者重新同步（下次按 canonical 重建），Session 状态
推送靠整表对账。
"""

from collections.abc import Callable

from app.config.logging.logger import log
from app.core.events.workflow_event import WorkflowEvent

EventSink = Callable[[WorkflowEvent], object]
"""接收执行事件的回调。"""

_sink: EventSink | None = None


def set_event_sink(sink: EventSink | None) -> None:
    """装配期注入进程级消费方；``None`` 解除（进程关闭后）。"""
    global _sink
    _sink = None if sink is None else _side_channel(sink)


def event_sink() -> EventSink:
    """取已注入的消费方（已包上旁路规则，不抛消费失败）。

    异常:
        RuntimeError: 尚未装配（lifespan 之外直接调用 core 而没有注入）。
    """
    if _sink is None:
        raise RuntimeError("event sink is not assembled")
    return _sink


def _side_channel(sink: EventSink) -> EventSink:
    def deliver(event: WorkflowEvent) -> object:
        try:
            return sink(event)
        except Exception:
            log.exception(
                "event_sink_failed",
                extra={
                    "msg": "执行事件消费失败，事实已提交，照常继续",
                    "data": {"event_type": event.type, "session_id": event.session_id},
                },
            )
            return None

    return deliver
