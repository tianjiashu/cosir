"""命令提交回执；业务接收与 SSE 订阅具有独立生命周期。"""

from pydantic import BaseModel

from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot


class CommandAccepted(BaseModel):
    """queued 表示本次提交未启动执行者；state 是提交时的读取快照。"""

    runId: int
    state: ConversationStateSnapshot
    queued: bool
