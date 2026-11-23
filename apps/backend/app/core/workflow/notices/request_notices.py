"""每次模型请求前推导的通知：提问作废、终端权限、配置变化、读过的文件变化。

这几项在驱动中途也会变、下一步就该让模型知道，所以 prepare_input 每次请求前都调用一次（取通知之前），
推出的通知与本批其余通知合成一条写入。比对基准都取上下文里模型实际看到的那条通知，不取进程内记忆。

与 ``drive_notices`` 不同，这里有一项带副作用：提问作废会先把占位推到 ``cancelled`` 并发卡片事件
（规则在 ``user_decision.decision_expiry``），通知随后才写入，两步不同事务；其余两项只读。
"""

from __future__ import annotations

from pathlib import Path

from app.core.permission.writable_root import absolute as absolute_writable_root
from app.core.tool.file_change_notices import file_change_notices
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody
from app.core.workflow.node_helper.common import _message_context, _runtime_context
from app.core.workflow.user_decision.decision_expiry import expire_on_user_input
from app.models.message_context.message_metadata.user_message_metadata import (
    TerminalPermissionBaseline,
)
from app.service import depends as service_depends


def request_notices(step_id: str) -> list[NoticeBody]:
    """本次模型请求前应投递的通知，按作废 → 终端权限 → 配置 → 文件的顺序；工作线程执行。"""
    notices = expire_on_user_input(step_id)
    for notice in (_terminal_permissions(), _configuration_changed(), _read_files_changed()):
        if notice is not None:
            notices.append(notice)
    return notices


def _configuration_changed() -> NoticeBody | None:
    """Agent 看到的配置变了就给一条净差异通知（基准见 ``configuration_change_notice``）。"""
    return _message_context().configuration_change_notice(_runtime_context().agent)


def _read_files_changed() -> NoticeBody | None:
    """读过的文件在读之后被别人改了就给一条通知；基准是上下文里最近一条同类通知的清单。"""
    context = _message_context()
    latest = context.latest_notice("read_files_changed")
    baseline = (latest[1].stale_reads or []) if latest is not None else []
    return file_change_notices().notice(
        _runtime_context().session.id, context.visible_tool_facts(), baseline
    )


def _terminal_permissions() -> NoticeBody | None:
    """有效终端权限或可写根与模型上次被告知的不同时说明（用户随时可切，下一条命令生效）。

    有效权限与可写根都取执行时的同一来源（``SessionService.effective_terminal_mode``、
    ``core.permission.writable_root``），基准是上下文里最近一条同类通知：没变就不说，跨 Run 也不重发。
    """
    rc = _runtime_context()
    mode = service_depends.get_session_service().effective_terminal_mode(rc.session.id)
    root = absolute_writable_root(Path(rc.workspace.root_path), rc.session.extra.writable_root)
    baseline = TerminalPermissionBaseline(mode=mode, writable_root=str(root))
    latest = _message_context().latest_notice("terminal_permissions")
    if latest is not None and latest[1].terminal_permission == baseline:
        return None
    content = (
        f"Terminal permissions now: {mode}. Base writable root: {root}. "
        "Your session scratch is also writable. "
        + (
            "Commands run with the current OS user permissions without a terminal sandbox. "
            if mode == "full_access"
            else "Commands run in an OS sandbox with IP networking off. Set escalate_sandbox on execute_terminal, "
            "saying why in intent, before starting a command that needs network or writes outside the writable "
            "root; the user must approve this exact command. The Cosir control directory stays protected. "
        )
        + "The user can change this at any time; you are told when it changes, and it applies to your next "
        "command. Approval never automatically reruns a failed command. Check partial effects before retrying. "
        "This mode affects Agent commands only; file tools, browser, MCP and user terminals have their own boundaries."
    )
    return NoticeBody(notice_kind="terminal_permissions", content=content, terminal_permission=baseline)
