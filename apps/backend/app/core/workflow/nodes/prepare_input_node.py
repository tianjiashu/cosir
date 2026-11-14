"""模型请求前的唯一关口：依次判定取消、挂起、步数上限，再把 mailbox 里的消息并入 context。

步与步之间的停止都在这里落定；模型请求中、工具执行中的取消仍由 model 节点与工具运行器就地中断。
mailbox 是进入 Run 的唯一通道：外部队列行的副本确认（删除外部队列行）与 context 写入同一事务提交，
消息不会丢也不会并入两次；执行内通知没有队列行，写入即可。全部通知（两种来源）先写、合成
一条 ``<system_notice>``，其余消息再按优先级逐条写。本节点不读外部队列、不按模型截断、不持
Session 闸门。Run 首次并入用户输入后，顺带启动标题支线（``branch.session_title``，每个 Run 至多
一次），它与主线并行、不影响流转。

需要直接执行的用户决定优先级最高、每轮只取一条：工具声明 ``user_decision == "execute"`` 时，
无论批准还是驳回，都确认并写下决定后绕过模型、直接执行原调用（先确认再执行：执行中途进程退出
不会重复执行），其余消息留到执行完回到这里再写。驳回也要执行：工具得落定自己的事实（如交接登记），
等待占位也只有执行结果能收尾。``reply`` 类工具的决定按 mailbox 的优先级与时间线和其余消息一起
交给模型；它没有执行结果，所以并入的同一事务就把等待占位推到 ``completed``，占位已不在等待
（已作答、已作废）的迟到决定只确认、不写入。并入前不必先闭合工具协议：``load_message`` 发请求前会
把被用户消息隔开的工具结果按调用恢复到合法顺序。
"""

import asyncio
from typing import cast

from sqlalchemy.orm import Session as DbSession

from app.config.configuration import get_tool_registry
from app.config.logging.logger import log
from app.core.events import InputConsumedEvent, ToolCallStatusChangedEvent
from app.core.workflow.branch import session_title
from app.core.workflow.decision_expiry import (
    expire_decisions,
    expired_notice,
    incoming_decision_effects,
)
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
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
from app.models.conversation_input import UserInputPresentation
from app.models.enums.react_route import ReactRoute
from app.models.enums.run_status import RunTransition
from app.models.message_metadata import TURN_INPUT_KINDS, ToolMessageMetadata
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

    decision = rc.mailbox.take_first(_executes_tool)
    if decision is not None:
        routed = await _route_decision(decision, step_id)
        if routed is not None:
            return {**route_state(state.step_count, ReactRoute.TOOLS), "batch": routed, "wait_for_input": False}
    await asyncio.to_thread(_expire_decisions_on_user_input, step_id)
    notices = rc.mailbox.take_notices()
    if notices:
        await asyncio.to_thread(_write_notices, notices)
    consumed_input = False
    while not rc.mailbox.isEmpty():
        message = rc.mailbox.pop()
        if not await asyncio.to_thread(_write_message, message, step_id=step_id):
            continue
        if isinstance(message.body, UserInputBody):
            consumed_input = True
            await asyncio.to_thread(session_title.name_untitled, rc, message.body.text)
    # 用户说了新话才值得重新起标题；副本在主线开始流式改写 working copy 前取好。
    patch: dict[str, object] = {**route_state(state.step_count, ReactRoute.MODEL), "batch": ToolBatch(), "wait_for_input": False}
    if consumed_input and session_title.should_launch(rc, state):
        session_title.launch(rc, await asyncio.to_thread(_message_context().load_message))
        patch["title_requested"] = True
    # 下一模型步不再看上一批观察或复用本次挂起请求。
    return patch


def _expire_decisions_on_user_input(step_id: str) -> None:
    """mailbox 里有用户输入时，先作废等用户回应的等待决定并告知模型；工作线程执行。

    放在取通知之前、而不是写那条输入的事务里：通知要与这批通知合成一条、排在输入前面，写输入时
    才结算就来不及了。代价是结算与输入不同事务——输入随后确认失败（极少：投递后、并入前被撤回）
    时提问已作废而输入没写入，可接受。排在这条输入前面的决定（先作答、后发消息）会先结算占位，
    所以它回答的调用不作废；排在后面的决定并入时发现占位已作废，只确认不写入。
    """
    rc = _runtime_context()
    effects = incoming_decision_effects(rc.mailbox.ordered())
    if effects.user_input is None:
        return
    for call_id in expire_decisions(step_id=step_id, keep=effects.answered_before_input):
        rc.mailbox.notify(expired_notice(call_id, "user_message"))


def _executes_tool(message: SessionMessage) -> bool:
    """这条消息是否是「需要绕过模型直接执行」的决定；去向由工具声明，不看批准还是驳回。"""
    if message.kind != "user_decision":
        return False
    body = cast(DecisionBody, message.body)
    definition = get_tool_registry().get_tool_definition(body.tool_name)
    return definition is not None and definition.user_decision == "execute"


async def _route_decision(decision: SessionMessage, step_id: str) -> ToolBatch | None:
    """确认并写下这条需要执行的决定；原调用仍在上下文时返回只含该调用的批次，否则交给模型。

    先确认再执行（最多执行一次）：确认删除、决定消息与「等待占位推到 ``running``」同一事务提交，
    之后才执行工具。占位离开 ``pending`` 后决定入口不再认它在等待，执行期间迟到的重复决定被拒，
    原调用不会被重放第二次；执行结果由 ``settle_decision_observation`` 推到终态。原调用找不到
    （如已被撤回移出上下文）时无法执行，决定已写入、交给模型并留痕。
    """
    rc = _runtime_context()
    body = cast(DecisionBody, decision.body)
    if not await asyncio.to_thread(_write_message, decision, step_id=step_id, claim_decision=True):
        return None
    claimed = _message_context().tool_result_metadata(body.tool_call_id)
    if claimed is not None and claimed.status == "running":
        emit(
            ToolCallStatusChangedEvent(
                session_id=rc.run.session_id,
                run_id=rc.run.id,
                step_id=step_id,
                tool_call_id=body.tool_call_id,
                status="running",
                display_data=claimed.display_data,
            )
        )
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


def _write_message(session_message: SessionMessage, *, step_id: str, claim_decision: bool = False) -> bool:
    """确认并写入一条外部队列消息，返回是否实际写入；工作线程执行。

    同一事务内先确认删除再写入，写入以确认返回的行内容为准；已被别的执行确认或被用户撤回的消息
    跳过、返回 ``False``。回合输入（用户输入、Agent 消息）在提交后发消费事件（带展示绑定、回合号与
    来源），供 Transport 投影出 user 消息。
    后台结论的认领、并入与原占位落定共用此事务；卡片事件只在提交后发布。
    ``claim_decision`` 只用于要执行的决定：同一事务把这次调用仍为 ``pending`` 的等待占位推到
    ``running``（占位已不在上下文时不动）。其余决定（``reply`` 类）在同一事务把占位推到
    ``completed``、载荷并进展示数据：决定入口当时校验通过不代表并入时仍在等待（提问可能已被新
    回合作废），所以这里再看一次，不在等待就只确认、不写入，返回 ``False``。
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
        if isinstance(current.body, DecisionBody) and not claim_decision:
            answered_event = _settle_answered(current.body, step_id=step_id, db=db)
        stale_decision = (
            isinstance(current.body, DecisionBody) and not claim_decision and answered_event is None
        )
        message, metadata = current.body.render()
        if stale_decision:
            log.info("prepare_input_stale_decision_dropped", extra={
                "msg": "决定并入时调用已不在等待，只确认不写入",
                "data": {"run_id": rc.run.id, "tool_call_id": metadata.tool_call_id},
            })
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
        if claim_decision:
            call_id = cast(DecisionBody, current.body).tool_call_id
            placeholder = context.tool_result_metadata(call_id)
            if placeholder is not None and placeholder.awaiting_decision:
                context.update_tool_result_metadata(
                    call_id, ToolMessageMetadata(status="running"), db=db
                )
        elif isinstance(current.body, BackgroundResultBody):
            background_event = settle_background_placeholder(
                current.body.tool_call_id, current.body, step_id=step_id, db=db
            )
    if answered_event is not None:
        emit(answered_event)
    if background_event is not None:
        log.info("background_placeholder_settled", extra={
            "msg": "后台结论已并入，占位已结算",
            "data": {"run_id": rc.run.id, "tool_call_id": background_event.tool_call_id,
                     "status": background_event.status},
        })
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


def _settle_answered(
    body: DecisionBody, *, step_id: str, db: DbSession
) -> ToolCallStatusChangedEvent | None:
    """``reply`` 类决定并入时结算等待占位，返回提交后才可发布的卡片事件；占位不在等待时返回 ``None``。

    这类工具没有执行结果，占位只能在这里收尾：推到 ``completed``（驳回也是——用户已经作出决定），
    决定与载荷并进展示数据，卡片据此显示回答。占位离开 ``pending`` 后决定入口不再认它在等待，
    重复提交被拒，「等你操作」的计数随状态事件更新。
    """
    rc = _runtime_context()
    context = _message_context()
    placeholder = context.tool_result_metadata(body.tool_call_id)
    if placeholder is None or not placeholder.awaiting_decision:
        return None
    written = context.update_tool_result_metadata(
        body.tool_call_id,
        ToolMessageMetadata(
            status="completed",
            display_data={
                **(placeholder.display_data or {}),
                "decision": body.decision,
                **(body.data or {}),
            },
        ),
        db=db,
    )
    return ToolCallStatusChangedEvent(
        session_id=rc.run.session_id,
        run_id=rc.run.id,
        step_id=step_id,
        tool_call_id=body.tool_call_id,
        status=written.status,
        display_data=written.display_data,
    )


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
