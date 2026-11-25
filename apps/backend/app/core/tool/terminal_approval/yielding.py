"""批准与用户操作的先后：新输入在先就不再要批准，新输入在后就撤销页面上的批准。

批准显示在最近的面向用户的会话上（子 Agent 没有界面，见 ``SessionService.display_session_id``）。
登记时的判定与新输入的投递在显示会话的闸门内串行，所以两者谁先谁后是确定的：

- :func:`admit`：工具线程登记前，页面上已有尚未并入的新输入（或这次调用已被取消），就不登记、直接让位；
- :func:`supersede`：actor 把新输入投进页面所属会话时调用，撤销页面上的全部批准。

子 Agent 没有新输入来接替它，让位或被撤销时停下它这次驱动，免得模型换个说法再申请；已启动的独立
后台工作不收回。用户停止与删除不经这里（见 ``registry.cancel_originated``）。

用户撤回输入同样在显示会话的闸门内（``SessionActor.withdraw_input``），所以闸门内看到的 mailbox 副本
就是仍在队列里的输入。显示会话自己的审批让位时把输入记为「已打断」（``mark_interrupted_work``），撤回时
模型才知道这次审批为什么没了；子 Agent 的不记，它的模型看不到父页面上那条输入。
"""

from __future__ import annotations

from app.core.tool.terminal_approval.registry import (
    PendingTerminalApproval,
    terminal_approvals,
)
from app.core.workflow.cancel.tool_call_cancellation_registry import should_stop_call


def admit(
    *,
    origin_session_id: int,
    run_id: int,
    tool_call_id: str,
    command: str,
    shell: str,
    cwd: str,
    intent: str,
) -> PendingTerminalApproval:
    """工具线程为原调用登记批准；让位时返回已收尾的请求（不登记、不进视图）。"""
    from app.service import depends as service_depends

    # 函数内导入：``session_runtime`` 物化 space 时会装配 actor，actor 又依赖本模块。
    from app.session_runtime.session_runtime_space_registry import session_runtime_spaces

    sessions = service_depends.get_session_service()
    display_id = sessions.display_session_id(origin_session_id)
    origin_title = sessions.get_session(origin_session_id).title
    request = PendingTerminalApproval(
        "", origin_session_id, display_id, run_id, tool_call_id, command, shell, cwd, intent, origin_title
    )
    space = session_runtime_spaces.get_or_create(display_id)
    with space.operation():
        if should_stop_call(origin_session_id, tool_call_id):
            return request.yielded("call_cancelled")
        # mailbox 只在驱动期间有内容（驱动收尾即清空）：这里看到的就是尚未并入的新输入。
        inputs = space.mailbox.user_input_ids()
        if inputs:
            if display_id == origin_session_id:
                service_depends.get_session_message_queue().mark_interrupted_work(display_id, inputs)
            else:
                session_runtime_spaces.get_or_create(origin_session_id).actor.request_cancel_drive(run_id)
            return request.yielded("user_message")
        return terminal_approvals.request(
            origin_session_id=origin_session_id,
            display_session_id=display_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            command=command,
            shell=shell,
            cwd=cwd,
            intent=intent,
            origin_title=origin_title,
        )


def supersede(display_session_id: int) -> bool:
    """用户输入已投进这个页面所属会话：撤销页面上的全部批准，原调用以取消收尾；返回是否撤销了这个
    会话自己的批准（调用方据此把输入记为「已打断」，子 Agent 的不算，理由见模块说明）。

    调用方持显示会话闸门、在事件循环上。子 Agent 的批准先停它所属的驱动、再解除等待，迟到的停止
    不误伤后来的 Run。
    """
    from app.session_runtime.session_runtime_space_registry import session_runtime_spaces

    own = False
    for request in terminal_approvals.displayed(display_session_id):
        if request.origin_session_id != display_session_id:
            origin = session_runtime_spaces.get_or_create(request.origin_session_id)
            origin.actor.cancel_drive(request.run_id)
        else:
            own = True
        terminal_approvals.cancel(request, "user_message")
    return own
