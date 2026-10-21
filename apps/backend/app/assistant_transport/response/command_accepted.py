"""命令提交回执；业务接收与 SSE 订阅具有独立生命周期。"""

from pydantic import BaseModel

from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot


class CommandAccepted(BaseModel):
    """``queued`` 表示消息只进了 inbox、本次未启动执行；``state`` 是提交后的读取快照。"""

    state: ConversationStateSnapshot
    queued: bool
