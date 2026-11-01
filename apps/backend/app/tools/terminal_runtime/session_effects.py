"""终端会话对应用层的副作用：snapshot 重投影与会话结束通知。

交接的代为落定在 ``handoff_registry``。

终端运行时只关心会话本身；凡是要触达 Transport 或 Task 外部队列的动作都集中在这里，
由 runtime 经 service 的单线程副作用执行器在会话锁外调用（失败由执行器记录，不影响会话）。进程单例
经 ``app.service.depends`` 与各自模块取用；函数内导入，避免终端包导入时拉起应用层。
"""

from __future__ import annotations


def refresh_task_sessions(task_id: int) -> None:
    """影响会话条目的变化（生命周期、集成、交接）：整表重投影该 Task 的终端会话列表。"""
    from app.service import depends

    depends.get_conversation_task_state_service().refresh_terminal_sessions(task_id)


def post_session_ended_notice(task_id: int, content: str) -> None:
    """Agent 不知情的会话结束作为系统通知投进 Task 外部队列。

    系统通知不唤醒 Run，随下一次推进并入：Agent 不必为此单独跑一轮，但下次行动前会知道。
    """
    from app.core.workflow.message_queue.task_message import NoticeBody, TaskMessage
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    task_runtime_spaces.enqueue(
        task_id, TaskMessage.of(NoticeBody(content=content, notice_kind="terminal_session_ended"))
    )
