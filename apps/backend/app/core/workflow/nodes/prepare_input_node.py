"""模型请求前的唯一关口：依次判定取消、挂起、步数上限，再把 mailbox 里的消息并入 context。

步与步之间的停止都在这里落定；模型请求中、工具执行中的取消仍由 model 节点与工具运行器就地中断。
mailbox 是进入 Run 的唯一通道：外部队列行的副本确认（删除外部队列行）与 context 写入同一事务提交，
消息不会丢也不会并入两次；执行内通知没有队列行，写入即可。写入前先取本次请求的通知
（``notices.request_notices``：提问作废、配置变化、读过的文件变化），与 mailbox 里的通知合成一条
``<system_notice>`` 先写，其余消息再按优先级逐条写。本节点不读外部队列、不按模型截断、不持
Session 闸门。Run 首次并入用户输入后，顺带启动标题支线（``branch.session_title``，每个 Run 至多
一次），它与主线并行、不影响流转。

用户决定（提问）按 mailbox 的优先级与时间线和其余消息一起交给模型；作废与结算规则在
``user_decision``，占位已不在等待的迟到决定只确认、不写入。
并入前不必先闭合工具协议：``load_message`` 发请求前会把被用户消息隔开的工具结果按调用恢复到合法顺序。
"""

import asyncio
from typing import cast

from app.config.logging.logger import log
from app.core.events import InputConsumedEvent
from app.core.workflow.branch import session_title
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.core.workflow.message_queue.session_message import (
    BackgroundResultBody,
    DecisionBody,
    NoticeBody,
    SessionMessage,
    UserInputBody,
    notices_to_context,
)
from app.core.workflow.node_helper.common import (
    RunStopped,
    _message_context,
    _runtime_context,
    emit,
    route_state,
)
from app.core.workflow.node_helper.finalize_max_steps import _finalize_max_steps
from app.core.workflow.node_helper.tool_observation_projection import (
    settle_background_placeholder,
)
from app.core.workflow.notices.request_notices import request_notices
from app.core.workflow.user_decision.decision_settlement import settle_answered
from app.models.message_context.message_metadata.base.message_kinds import TURN_INPUT_KINDS
from app.models.message_context.message_metadata.user_message_metadata import UserInputPresentation
from app.models.run.run_status import RunTransition
from app.service import depends as service_depends
from app.storage.store_engines import main_db_factory


async def _prepare_input_node(state: ReactExecutionState) -> dict[str, object]:
    """按「取消 → 挂起 → 步数上限 → 并入消息」的顺序判定，通过后流转到 model 节点。

    挂起请求来自工具观察（``wait_messages``、等审批的 HITL 调用），读完即清空。
    取消优先于挂起：工具要求等待时 Run 恰好被停，按取消落定。挂起先于步数上限：Agent 明确在等
    消息时不按「步数用完」失败，唤醒后回到这里再检查上限。挂起前 mailbox 已有可唤醒消息（如子 Agent
    结论在工具执行期间送达）就直接继续，省掉「挂起 → 复查 → 唤醒」一圈；之后才到达的消息仍由 actor
    的挂起后复查兜住。是否可唤醒与调度规则同一条（``SessionMessage.wakes``）：多数通知不算，派生工作
    被停止这类告知算——它在挂起前一刻到达时，挂起就再没有人来叫醒。前三类落定都不写入：
    外部队列行留给后续执行，执行内通知随 mailbox 重置丢弃。
    """
    rc = _runtime_context()
    step_count = state.step_count + 1
    step_id = f"step-{step_count}"
    if rc.is_cancelled():
        _cancel_before_request(step_id, rc.run.id)
    if state.wait_for_input:
        if not rc.mailbox.has_wakeable(rc.run.id):
            _suspend(step_id, rc.run.id)
            return {"batch": ToolBatch(), "wait_for_input": False, "next_node": ReactRoute.END}
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

    for notice in await asyncio.to_thread(request_notices, step_id):
        rc.mailbox.notify(notice)
    notices = rc.mailbox.take_notices()
    if notices:
        await asyncio.to_thread(_write_notices, notices)
    consumed_input = False
    while not rc.mailbox.isEmpty():
        message = rc.mailbox.pop()
        if not await asyncio.to_thread(_write_message, message, step_id=step_id):
            continue
        consumed_input = consumed_input or isinstance(message.body, UserInputBody)
    # 下一模型步不再看上一批观察或复用本次挂起请求。
    patch: dict[str, object] = {
        **route_state(state.step_count, ReactRoute.MODEL),
        "batch": ToolBatch(),
        "wait_for_input": False,
    }
    # 用户说了新话才值得重新起标题。
    if consumed_input and await session_title.start_after_input(rc, state):
        patch["title_requested"] = True
    return patch


def _write_notices(notices: list[SessionMessage]) -> None:
    """把这一批通知合成一条 ``<system_notice>`` 写入；工作线程执行。

    其中的外部队列行与写入同一事务确认，写入以确认返回的行内容为准，已被别的执行确认或被撤回的
    跳过；执行内通知没有行，原样写入。一条都不剩时不写。
    """
    row_ids = [notice.id for notice in notices if notice.id is not None]
    with main_db_factory().begin() as db:
        acked = service_depends.get_session_message_queue().ack(db, row_ids) if row_ids else {}
        bodies = [
            cast(NoticeBody, (acked[notice.id] if notice.id is not None else notice).body)
            for notice in notices
            if notice.id is None or notice.id in acked
        ]
        if not bodies:
            return
        message, metadata = notices_to_context(bodies)
        _message_context().add_message(
            message, metadata=metadata, db=db, project_to_transport=False
        )


def _write_message(session_message: SessionMessage, *, step_id: str) -> bool:
    """确认并写入一条外部队列消息，返回是否实际写入；工作线程执行。

    同一事务内先确认删除再写入，写入以确认返回的行内容为准；已被别的执行确认或被用户撤回的消息
    跳过、返回 ``False``。回合输入（用户输入、Agent 消息）在提交后发消费事件（带展示绑定、回合号与
    来源），供 Transport 投影出 user 消息。
    后台结论的认领、并入与原占位落定共用此事务；卡片事件只在提交后发布。
    用户决定在同一事务把占位推到 ``completed``、载荷并进展示数据：决定入口当时校验通过不代表并入时
    仍在等待（提问可能已被新回合作废），所以这里再看一次，不在等待就只确认、不写入，返回 ``False``。
    """
    rc = _runtime_context()
    context = _message_context()
    background_event = None
    answered_event = None
    with main_db_factory().begin() as db:
        acked = service_depends.get_session_message_queue().ack(db, [cast(int, session_message.id)])
        current = acked.get(cast(int, session_message.id))
        if current is None:
            return False
        if isinstance(current.body, DecisionBody):
            answered_event = settle_answered(current.body, step_id=step_id, db=db)
        stale_decision = isinstance(current.body, DecisionBody) and answered_event is None
        message, metadata = current.body.render()
        if stale_decision:
            log.info(
                "prepare_input_stale_decision_dropped",
                extra={
                    "msg": "决定并入时调用已不在等待，只确认不写入",
                    "data": {
                        "run_id": rc.run.id,
                        "tool_call_id": cast(DecisionBody, current.body).tool_call_id,
                    },
                },
            )
            return False
        # 回合输入以队列行 id 为消息身份：快照里的用户消息与途中来信都按它寻址。
        is_turn_input = metadata.kind in TURN_INPUT_KINDS
        context.add_message(
            message,
            metadata=metadata,
            db=db,
            project_to_transport=current.project_to_transport,
            message_id=current.id if is_turn_input else None,
        )
        # 刚写下的这一行所属的回合，交给消费事件：实时投影与冷重建读同一个回合号。
        turn = cast(int, context.current_turn)
        if isinstance(current.body, BackgroundResultBody):
            background_event = settle_background_placeholder(
                current.body.tool_call_id, current.body, step_id=step_id, db=db
            )
    if answered_event is not None:
        emit(answered_event)
    if background_event is not None:
        log.info(
            "background_placeholder_settled",
            extra={
                "msg": "后台结论已并入，占位已结算",
                "data": {
                    "run_id": rc.run.id,
                    "tool_call_id": background_event.tool_call_id,
                    "status": background_event.status,
                },
            },
        )
        emit(background_event)
    if is_turn_input:
        emit(
            InputConsumedEvent(
                session_id=rc.session.id,
                run_id=rc.run.id,
                message_id=cast(int, current.id),
                presentation=cast(UserInputPresentation, metadata.presentation),
                turn=turn,
                source=metadata.source,
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
    service_depends.get_run_ledger().transition(
        run_id,
        RunTransition.CANCEL,
        end_reason=rc.cancel_end_reason(),
        usage_stats=rc.usage_stats,
    )
    raise RunStopped()


def _suspend(step_id: str, run_id: int) -> None:
    """有挂起请求且没有现成的可唤醒消息：迁移为 ``suspended``，由 actor 收尾并复查唤醒。"""
    rc = _runtime_context()
    suspended = service_depends.get_run_ledger().transition(
        run_id, RunTransition.SUSPEND, usage_stats=rc.usage_stats
    )
    log.info(
        "prepare_input_suspended",
        extra={
            "msg": f"Run 挂起，等待消息唤醒，step_id={step_id}",
            "data": {"step_id": step_id, "run_id": run_id, "applied": suspended is not None},
        },
    )
