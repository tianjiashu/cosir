"""终端会话对应用层的全部副作用：snapshot 重投影、会话结束通知、交接代为落定。

终端运行时只关心会话本身；凡是要触达 Transport、Task 外部队列或 HITL 决定的动作都集中在这里，
由 runtime 经 service 的单线程副作用执行器在会话锁外调用（失败由执行器记录，不影响会话）。进程单例
经 ``app.service.depends`` 与各自模块取用；函数内导入，避免终端包导入时拉起应用层。
"""

from __future__ import annotations

from app.config.logging.logger import log


def refresh_task_sessions(task_id: int) -> None:
    """影响会话条目的变化（生命周期、集成、租约、交接）：整表重投影该 Task 的终端会话列表。"""
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


def settle_handoff(task_id: int, tool_call_id: str) -> None:
    """会话在交接中结束时代用户驳回那次 ``terminal_handoff``，唤醒等它的 Agent（决定会写 SQLite）。

    用户可能恰好已经交还：决定已提交或已不在等待时只记录，不重复。
    """
    from app.core.task_actor import SessionConflictError
    from app.service import depends

    try:
        depends.get_tool_decision_service().submit(
            task_id,
            tool_call_id,
            "reject",
            feedback="the terminal session ended before the user handed it back",
            source="runtime",
        )
    except SessionConflictError as exc:
        log.info(
            "terminal_handoff_settle_skipped",
            extra={
                "msg": "会话结束时交接已有决定或已不在等待，跳过代为落定",
                "data": {"task_id": task_id, "tool_call_id": tool_call_id, "code": exc.code},
            },
        )
