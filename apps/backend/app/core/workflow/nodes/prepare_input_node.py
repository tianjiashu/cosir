"""模型请求前的唯一关口：依次判定取消、挂起、步数上限，再把 mailbox 里的消息并入 context。

步与步之间的停止都在这里落定；模型请求中、工具执行中的取消仍由 model 节点与工具运行器就地中断。
投递归 Task 的调度 actor（外部队列消息）与各节点（Run 内通知），本节点只取光 mailbox 并写入：
持久化消息的确认（删除外部队列行）与 context 写入同一事务提交，消息不会丢也不会并入两次；不读
外部队列、不按模型截断、不持 Task 闸门。并入前不必先闭合工具协议：``load_message`` 发请求前会
把被用户消息隔开的工具结果按调用恢复到合法顺序。
"""

import asyncio
from typing import cast

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.events import UserInputConsumedEvent
from app.models.tool_batch import ToolBatch
from app.models.react_execution_state import ReactExecutionState
from app.models.enums.react_route import ReactRoute
from app.core.workflow.node_helper.common import (
    RunStopped,
    _message_context,
    _runtime_context,
    emit,
    route_state,
)
from app.core.workflow.node_helper.finalize_max_steps import _finalize_max_steps
from app.models.enums.conversation_run_status import RunTransition
from app.models.task_message import (
    NoticeBody,
    TaskMessage,
    UserInputBody,
    notices_to_context,
    tagged_to_context,
    user_input_to_context,
)
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory


async def _prepare_input_node(state: ReactExecutionState) -> dict[str, object]:
    """按「取消 → 挂起 → 步数上限 → 并入消息」的顺序判定，通过后流转到 model 节点。

    挂起请求取自执行状态里上一批的观察（``batch.suspends()``），读完即清空批次。
    取消优先于挂起：工具要求等待时 Run 恰好被停，按取消落定。挂起先于步数上限：Agent 明确在等
    消息时不按「步数用完」失败，唤醒后回到这里再检查上限。挂起前 mailbox 已有外部可唤醒消息（如
    子 Agent 结论在工具执行期间送达）就直接继续，省掉「挂起 → 复查 → 唤醒」一圈；之后才到达的消息
    仍由 actor 的挂起后复查兜住。前三类落定都不取 mailbox：持久化消息留在外部队列给后续执行，Run 内
    通知随执行丢弃。
    """
    rc = _runtime_context()
    step_count = state.step_count + 1
    step_id = f"step-{step_count}"
    if rc.is_cancelled():
        _cancel_before_request(step_id, rc.run.id)
    if state.batch.suspends():
        if not rc.mailbox.has_external():
            _suspend(step_id, rc.run.id)
            return {"batch": ToolBatch(), "next_node": ReactRoute.END}
        log.info(
            "prepare_input_suspend_skipped",
            extra={
                "msg": f"挂起前已有可唤醒消息，直接继续，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": rc.run.id},
            },
        )
    # 超配额时不再发起推理，落定 max_steps_reached 失败终态。
    if step_count > state.step_limit:
        return await _finalize_max_steps(step_count, state.step_limit)
    messages = rc.mailbox.drain()
    if messages:
        consumed = await asyncio.to_thread(_write_messages, messages)
        # 提交后才投影：消费事件按身份把排队消息搬进 Run。
        for message in consumed:
            emit(
                UserInputConsumedEvent(
                    task_id=rc.task.id, run_id=rc.run.id, message_id=cast(int, message.id)
                )
            )
    # 本节点是批次的唯一清空点：挂起判定已读完，下一模型步不能再看到上一批观察。
    return {**route_state(state.step_count, ReactRoute.MODEL), "batch": ToolBatch()}


def _write_messages(messages: list[TaskMessage]) -> list[TaskMessage]:
    """一个事务内确认持久化消息并写入 context，返回实际并入且需投影的用户输入；工作线程执行。

    先确认后写入：确认返回删除那一刻的行内容（排队期间的原地编辑以它为准），已被确认过的消息
    跳过。通知合成一条消息排在最前，其余消息（用户输入、决定、后台结果）按入队顺序各写一条，让
    它们成为模型最后看到的内容；落库后成为历史固定行，后续请求前缀不分叉。
    """
    context = _message_context()
    with main_session_factory().begin() as session:
        acked = service_depends.get_task_message_queue().ack(
            session, [m.id for m in messages if m.id is not None]
        )
        live = [
            m if m.id is None else acked[m.id]
            for m in messages
            if m.id is None or m.id in acked
        ]
        notices = [m for m in live if isinstance(m.body, NoticeBody)]
        if notices:
            message, metadata = notices_to_context([cast(NoticeBody, m.body) for m in notices])
            context.add_message(
                message,
                metadata=metadata,
                session=session,
                project_to_transport=any(m.project_to_transport for m in notices),
            )
        for item in live:
            body = item.body
            if isinstance(body, NoticeBody):
                continue
            message, metadata = (
                user_input_to_context(body)
                if isinstance(body, UserInputBody)
                else tagged_to_context(body)
            )
            context.add_message(
                message,
                metadata=metadata,
                message_id=item.id,
                session=session,
                project_to_transport=item.project_to_transport,
            )
        users = [m for m in live if isinstance(m.body, UserInputBody)]
    if len(live) != len(messages):
        log.warning(
            "prepare_input_skipped_acked_messages",
            extra={
                "msg": "mailbox 中有已被确认的消息，跳过不重复并入",
                "data": {
                    "message_ids": [
                        m.id for m in messages if m.id is not None and m.id not in acked
                    ]
                },
            },
        )
    return [user for user in users if user.project_to_transport]


def _cancel_before_request(step_id: str, run_id: int) -> None:
    """在发起模型请求前落定 cancelled 并结束本次驱动。"""
    rc = _runtime_context()
    log.info(
        "model_node_cancelled_before_request",
        extra={
            "msg": f"模型请求前检测到 run 已取消，跳过模型调用，step_id={step_id}",
            "data": {"step_id": step_id, "run_id": run_id},
        },
    )
    service_depends.get_conversation_run_state_service().transition(
        run_id,
        RunTransition.CANCEL,
        end_reason=Constant.Run.RUN_END_REASON_RUNTIME_CANCELLED,
        usage_stats=rc.usage_stats,
    )
    raise RunStopped()


def _suspend(step_id: str, run_id: int) -> None:
    """工具要求等待且没有现成的可唤醒消息：迁移为 ``suspended``，由 actor 收尾并复查唤醒。"""
    rc = _runtime_context()
    suspended = service_depends.get_conversation_run_state_service().transition(
        run_id, RunTransition.SUSPEND, usage_stats=rc.usage_stats
    )
    log.info(
        "prepare_input_suspended",
        extra={
            "msg": f"工具要求挂起，Run 等待消息唤醒，step_id={step_id}",
            "data": {"step_id": step_id, "run_id": run_id, "applied": suspended is not None},
        },
    )
