"""会话事件的统一投递入口。

事件有两条投递通道，**生效时机**不同：

- **LangGraph custom stream**：节点内产生的事件先入队，随该节点输出一起流出（消费端是
  ``ReactLikeWorkflow.run`` 的 ``graph.astream`` 循环）；与同一节点内其他入队事件**顺序一致**。
- **直接投影**：不在 graph 上下文时（API 取消、启动恢复、冷读补写）立即写入进程内 snapshot。

两者混用会让顺序倒置：直投立即生效，而入队事件要等节点返回才被消费，于是「节点尾部入队的
增量」会晚于「同节点内直投的终态」到达投影器，被终态白名单丢弃（表现为「长回复最后一段不
显示、重启后的冷重建却完整」）。因此 **graph 内产生的事件一律入队**，只有无法入队时才回退
直投——顺序正确优先于终态事件的毫秒级时延，代价是要求客户端在 run 到达终态前保持订阅。
"""

from __future__ import annotations

from typing import Literal

from langgraph.config import get_stream_writer

from app.assistant_transport.event.conversation_event import ConversationEvent
from app.config.logging.logger import log

DispatchChannel = Literal["stream", "direct"]
"""事件实际使用的投递通道。"""

# 非 graph 上下文调用 ``get_stream_writer`` 时 LangGraph 的表现：它从 RunnableConfig 读取
# 运行时上下文（``get_config()[CONF][CONFIG_KEY_RUNTIME]``）。没有 runnable context 时
# ``get_config`` 抛 ``RuntimeError("Called get_config outside of a runnable context")``；
# 有 config 但缺对应配置键时抛 ``KeyError``；RuntimeConfig 形态异常时抛 ``TypeError``。
# 三者都属于「当前不可入队」，应回退直投，而不是让状态迁移整体失败。
_MISSING_GRAPH_CONTEXT_ERRORS = (KeyError, TypeError, RuntimeError)


def dispatch_conversation_event(event: ConversationEvent) -> DispatchChannel:
    """投递一条会话事件，优先与 graph 内增量共用同一有序通道。

    参数:
        event: 待投递的会话事件。

    返回:
        ``"stream"``：已写入 LangGraph custom stream，由消费循环按产生顺序投影；
        ``"direct"``：当前不在 graph 上下文，已直接投影。

    异常:
        无；直接投影自身的异常向上传播。

    副作用:
        ``stream`` 分支只入队，不立即投影；``direct`` 分支立即更新进程内 snapshot 并通知
        订阅者，并记一条 debug 日志说明为何未能入队。
    """

    try:
        writer = get_stream_writer()
    except _MISSING_GRAPH_CONTEXT_ERRORS:
        writer = None
    if writer is not None:
        writer(event)
        return "stream"
    from app.service.depends import get_conversation_event_projector

    log.debug(
        "conversation_event_dispatched_direct",
        extra={
            "msg": "当前不在 graph 上下文，事件直接投影",
            "data": {
                "event_type": event.type,
                "task_id": event.task_id,
                "run_id": event.run_id,
            },
        },
    )
    get_conversation_event_projector().process(event)
    return "direct"


__all__ = ["DispatchChannel", "dispatch_conversation_event"]
