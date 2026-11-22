"""每次模型请求前推导的通知：提问作废、配置变化、读过的文件变化。

这几项在驱动中途也会变、下一步就该让模型知道，所以 prepare_input 每次请求前都调用一次（取通知之前），
推出的通知与本批其余通知合成一条写入。比对基准都取上下文里模型实际看到的那条通知，不取进程内记忆。

与 ``drive_notices`` 不同，这里有一项带副作用：提问作废会先把占位推到 ``cancelled`` 并发卡片事件
（规则在 ``user_decision.decision_expiry``），通知随后才写入，两步不同事务；其余两项只读。
"""

from __future__ import annotations

from app.core.file_access.read_change_notice import reconcile_read_changes
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody
from app.core.workflow.node_helper.common import _message_context, _runtime_context
from app.core.workflow.user_decision.decision_expiry import expire_on_user_input


def request_notices(step_id: str) -> list[NoticeBody]:
    """本次模型请求前应投递的通知，按作废 → 配置 → 文件的顺序；工作线程执行。"""
    notices = expire_on_user_input(step_id)
    for notice in (_configuration_changed(), _read_files_changed()):
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
    return reconcile_read_changes(_runtime_context().session.id, context.file_knowledge(), baseline)
