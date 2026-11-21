"""会话管理工具的寻址规则：发送认整个工作区，读取认整棵派生树，停止只认直接派生的一层。

发送不看派生关系：同级会话之间回复 ``<agent_message>`` 也用它；投递只是给对方一条消息，不授予控制对方的
权限（对方怎么推进归它自己的 actor），所以范围可以最宽。

读取与停止为什么分开：读取没有副作用，子 Agent 挂起在等孙 Session 时，派生方直接看一眼孙 Session 卡在哪，比
绕回去问子 Agent 少一轮。停止不能越级：上游停止（``parent``）不汇报也不留通知（``derived_report``），
越级停掉孙 Session 时，正在 ``wait_messages`` 上等它的子 Agent 没有任何消息能唤醒、会一直挂着；停直接
下级则会沿收回逐层向下（``SessionRecall``）。各工具与 ``session_list`` 的标注都只读这里。
"""

from app.core.tool.results.tool_error import tool_error
from app.core.tool.schemas import ToolExecutionContext, ToolObservation
from app.models import SessionRecord
from app.service.depends import get_session_service


def find_recipient(
    tool_name: str, session_id: int, execution_context: ToolExecutionContext
) -> SessionRecord | ToolObservation:
    """返回同一工作区里的另一个 Session；指向自己、不存在或在别的工作区，返回错误观察。"""
    session = _lookup(tool_name, session_id, execution_context)
    if isinstance(session, ToolObservation):
        return session
    if session.workspace_id != execution_context.workspace_id:
        return tool_error(
            tool_name, f"session {session_id} is in another workspace; choose a session in this workspace."
        )
    return session


def find_readable_session(
    tool_name: str, session_id: int, execution_context: ToolExecutionContext
) -> SessionRecord | ToolObservation:
    """返回调用方派生树里的那个 Session（任意层）；否则返回错误观察。"""
    session = _lookup(tool_name, session_id, execution_context)
    if isinstance(session, ToolObservation):
        return session
    if not get_session_service().derives_from(session, execution_context.session_id):
        return tool_error(
            tool_name,
            f"session {session_id} is not in your derivation tree; you can read only sessions listed by session_list.",
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
