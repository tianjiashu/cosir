"""会话管理工具的寻址规则：发送与读取认整个工作区，停止只认直接派生的一层。

发送与读取为什么可以最宽：投递只是给对方一条消息，不授予控制对方的权限（对方怎么推进归它自己的 actor）；
读取没有副作用。两者范围必须一致——能发消息却看不见对方，Agent 就只能靠来信里的 ``source_session_id``
或用户报出 id 才找得到同工作区的其他会话。子 Agent 挂起在等孙 Session 时，派生方直接看一眼孙 Session 卡在
哪，也比绕回去问子 Agent 少一轮。

停止不能越级：上游停止（``parent``）不汇报也不留通知（``derived_report``），越级停掉孙 Session 时，正在
``wait_messages`` 上等它的子 Agent 没有任何消息能唤醒、会一直挂着；停直接下级则会沿收回逐层向下
（``SessionRecall``）。停掉用户侧栏里别的会话则是越权。各工具与 ``session_list`` 的标注都只读这里。
"""

from app.core.tool.results.tool_error import tool_error
from app.core.tool.schemas import ToolExecutionContext, ToolObservation
from app.models.session.session_record import SessionRecord
from app.service.depends import get_session_service


def find_workspace_session(
    tool_name: str, session_id: int, execution_context: ToolExecutionContext
) -> SessionRecord | ToolObservation:
    """``session_send`` 与 ``session_read`` 共用：返回同一工作区里的另一个 Session；指向自己、不存在或在别的
    工作区，返回错误观察。"""
    session = _lookup(tool_name, session_id, execution_context)
    if isinstance(session, ToolObservation):
        return session
    if session.workspace_id != execution_context.workspace_id:
        return tool_error(
            tool_name, f"session {session_id} is in another workspace; choose a session in this workspace."
        )
    return session


def find_stoppable_session(
    tool_name: str, session_id: int, execution_context: ToolExecutionContext
) -> SessionRecord | ToolObservation:
    """返回调用方直接派生的那个 Session；更深一层的指回它的派生方，其余返回错误观察。"""
    session = _lookup(tool_name, session_id, execution_context)
    if isinstance(session, ToolObservation):
        return session
    if can_stop(session, execution_context.session_id):
        return session
    if get_session_service().derives_from(session, execution_context.session_id):
        return tool_error(
            tool_name,
            f"session {session_id} was derived by session {session.deriver_id()}, not by you; only its "
            "deriver can stop it. Stop that session instead if you no longer need any of its work, or "
            "ask it with session_send.",
        )
    return tool_error(
        tool_name,
        f"session {session_id} is not in your derivation tree; you can stop only sessions you derived directly.",
    )


def can_stop(session: SessionRecord, caller_id: int) -> bool:
    """``session_list`` 标注用：与 :func:`find_stoppable_session` 同一条规则。"""
    return session.derived_by(caller_id)


def _lookup(
    tool_name: str, session_id: int, execution_context: ToolExecutionContext
) -> SessionRecord | ToolObservation:
    if session_id == execution_context.session_id:
        return tool_error(tool_name, "session_id is your own session; choose another session.")
    try:
        return get_session_service().get_session(session_id)
    except KeyError:
        return tool_error(tool_name, f"session not found: {session_id}; it may have been deleted.")
