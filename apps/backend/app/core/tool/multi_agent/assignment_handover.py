"""派生工具交出任务说明：投递之后与发起方的收回对账，派生出的 Session 不能躲过收回。"""

from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry
from app.core.workflow.message_queue.session_message import SessionMessage
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces


def hand_over_assignment(deriver_id: int, derived_id: int, assignment: SessionMessage) -> bool:
    """把任务说明投给刚建出的 Session；发起方正被收回时一并收回它，返回 ``False``（调用方按取消收尾）。

    收回先打 ``parent`` 信号、再列派生 Session（``SessionActor._cancel``）；这里先提交建会话与投递、再看
    信号，两边的先后因此只有两种：这里看到信号，就自己收回；没看到，信号晚于这里的提交，收回列得到它。
    信号查在派生之前挡不住：查过之后、建出之前被收回，列表里还没有它，它会照常跑完、向已收回的发起方汇报。
    工作线程里调用（收回要写库）。
    """
    session_runtime_spaces.enqueue(derived_id, assignment)
    if cancellation_registry.reason(deriver_id) != "parent":
        return True
    session_runtime_spaces.get_or_create(derived_id).actor.request_recall()
    return False
