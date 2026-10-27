"""模型请求前的唯一关口：依次判定取消、挂起、步数上限，再把 mailbox 里的消息并入 context。

步与步之间的停止都在这里落定；模型请求中、工具执行中的取消仍由 model 节点与工具运行器就地中断。
投递归 Task 的调度 actor（外部队列消息）与各节点（Run 内通知），本节点只取 mailbox 并写入：
持久化消息的确认（删除外部队列行）与 context 写入同一事务提交，消息不会丢也不会并入两次；不读
外部队列、不按模型截断、不持 Task 闸门。Run 首次并入用户输入后，顺带启动标题支线
（``branch.task_title``，每个 Run 至多一次），它与主线并行、不影响流转。

需要直接执行的用户决定优先级最高、每轮只取一条：工具声明 ``user_decision == "execute"`` 时，
无论批准还是驳回，都确认并写下决定后绕过模型、直接执行原调用（先确认再执行：执行中途进程退出
不会重复执行），其余消息留到执行完回到这里再写。驳回也要执行：工具得落定自己的事实（如 pending
TeamRun），等待占位也只有执行结果能收尾。``reply`` 类工具的决定只是普通消息，按 mailbox 的
优先级与时间线和其余消息一起交给模型。并入前不必先闭合工具协议：``load_message`` 发请求前会
把被用户消息隔开的工具结果按调用恢复到合法顺序。
"""

import asyncio
from typing import cast

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.events import UserInputConsumedEvent
from app.core.workflow.branch import task_title
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.config.configuration import get_tool_registry
from app.core.workflow.inbox.task_message import DecisionBody
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
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
from app.core.workflow.inbox.task_message import (
    TaskMessage,
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

    decision = rc.mailbox.take_first(_executes_tool)
    if decision is not None:
        routed = await _route_decision(decision, step_id)
        if routed is not None:
            return {**route_state(state.step_count, ReactRoute.TOOLS), "batch": routed}
    consumed_input = False
    while not rc.mailbox.isEmpty():
        message = rc.mailbox.pop()
        if await asyncio.to_thread(_write_message, message) and message.kind == "user_input":
            consumed_input = True
    # 用户说了新话才值得重新起标题；副本在主线开始流式改写 working copy 前取好。
    patch: dict[str, object] = {**route_state(state.step_count, ReactRoute.MODEL), "batch": ToolBatch()}
    if consumed_input and task_title.should_launch(rc, state):
        task_title.launch(rc, await asyncio.to_thread(_message_context().load_message))
        patch["title_requested"] = True
    # 本节点是批次的唯一清空点：挂起判定已读完，下一模型步不能再看到上一批观察。
    return patch


def _executes_tool(message: TaskMessage) -> bool:
    """这条消息是否是「需要绕过模型直接执行」的决定；去向由工具声明，不看批准还是驳回。"""
    if message.kind != "user_decision":
        return False
    body = cast(DecisionBody, message.body)
    definition = get_tool_registry().get_tool_definition(body.tool_name)
    return definition is not None and definition.user_decision == "execute"


async def _route_decision(decision: TaskMessage, step_id: str) -> ToolBatch | None:
    """确认并写下这条需要执行的决定；原调用仍在上下文时返回只含该调用的批次，否则交给模型。

    先确认再执行（最多执行一次）：确认删除与决定消息同一事务提交，之后才执行工具。原调用找不到
    （如已被编辑重跑移出上下文）时无法执行，决定已写入、交给模型并留痕。
    """
    rc = _runtime_context()
    body = cast(DecisionBody, decision.body)
    if not await asyncio.to_thread(_write_message, decision):
        return None
    call = _message_context().find_tool_call(body.tool_call_id)
    if call is None:
        log.warning(
            "prepare_input_decision_call_missing",
            extra={
                "msg": f"用户决定的工具调用已不在上下文中，只把决定交给模型，step_id={step_id}",
                "data": {
                    "step_id": step_id,
                    "run_id": rc.run.id,
                    "tool_call_id": body.tool_call_id,
                    "tool_name": body.tool_name,
                },
            },
        )
        return None
    log.info(
        "prepare_input_decision_routed_to_tool",
        extra={
            "msg": f"用户已决定，绕过模型直接执行原调用，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "run_id": rc.run.id,
                "tool_call_id": body.tool_call_id,
                "tool_name": body.tool_name,
            },
        },
    )
    return ToolBatch(calls=[call], decision=body.model_dump(mode="json"))


def _write_message(task_message: TaskMessage) -> bool:
    """写入一条消息，返回是否实际写入；工作线程执行。

    外部队列来的消息（``id`` 非空）在同一事务内先确认删除再写入：确认返回删除那一刻的行内容
    （排队期间的原地编辑以它为准），已被别的执行确认过的消息跳过、返回 ``False``。Run 内通知
    （``id`` 为 ``None``）只是一条写入，不需要事务。用户输入在提交后发消费事件，供排队投影收敛。
    """
    rc = _runtime_context()
    context = _message_context()
    if task_message.id is None:
        message, metadata = task_message.body.render()
        context.add_message(message, metadata=metadata, project_to_transport=False)
        return True
    with main_session_factory().begin() as session:
        acked = service_depends.get_task_message_queue().ack(session, [task_message.id])
        current = acked.get(task_message.id)
        if current is None:
            return False
        message, metadata = current.body.render()
        context.add_message(
            message,
            metadata=metadata,
            session=session,
            project_to_transport=current.project_to_transport,
            message_id=current.id if current.kind == "user_input" else None,
        )
    if current.kind == "user_input":
        emit(
            UserInputConsumedEvent(
                task_id=rc.task.id, run_id=rc.run.id, message_id=cast(int, current.id)
            )
        )
    return True


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
    service_depends.get_conversation_run_ledger().transition(
        run_id,
        RunTransition.CANCEL,
        end_reason=Constant.Run.RUN_END_REASON_RUNTIME_CANCELLED,
        usage_stats=rc.usage_stats,
    )
    raise RunStopped()


def _suspend(step_id: str, run_id: int) -> None:
    """工具要求等待且没有现成的可唤醒消息：迁移为 ``suspended``，由 actor 收尾并复查唤醒。"""
    rc = _runtime_context()
    suspended = service_depends.get_conversation_run_ledger().transition(
        run_id, RunTransition.SUSPEND, usage_stats=rc.usage_stats
    )
    log.info(
        "prepare_input_suspended",
        extra={
            "msg": f"工具要求挂起，Run 等待消息唤醒，step_id={step_id}",
            "data": {"step_id": step_id, "run_id": run_id, "applied": suspended is not None},
        },
    )
