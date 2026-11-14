"""「等用户回应」的等待决定何时过期、怎么过期、怎么告诉模型（``ToolDefinition.decision_lifetime
== "until_user_input"``，目前是提问）。整条规则只在这里。

这类决定问的是「此刻做不做这件事」：用户作答、跳过，或者不理它、直接说了别的话——那句话就是
回应。它不按回合算：别的 Agent 发来的消息同样开启回合，却与用户答不答无关；Run 正常完成后提问
仍在，回答可以落进之后的回合。四处结束它：

- ``prepare_input`` 并入一条用户输入之前：用户没答、直接说了新的话；
- observe 写下同类的新等待占位时：模型自己又问了一次（典型是停止后恢复），旧的不再有人答；
- Run 被用户停下或失败（``SessionActor`` 的落定点）：那一回合不会再往下走；
- 后端重启收敛了运行中的 Run（``RecoverService``）。

前两处在 Run 内，经 context manager 把内存工作副本与库一起改（:func:`expire_decisions`）；后
两处没有驱动在跑，直接改库（:func:`expire_decisions_after_run`）。Run 正常完成不过期，理由见
``SessionActor._expire_decisions``。

过期只读工具声明，不按工具名分支。占位推到 ``cancelled`` 并带短提示，正文不变——它仍是这次调用
在模型协议上的结果；等待占位说过「结果另行送达」，所以除了模型自己重复提问，都要另给一条通知
（:func:`expired_notice`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from app.config.configuration import get_tool_registry
from app.config.logging.logger import log
from app.core.events import ToolCallStatusChangedEvent
from app.core.tool.placeholder_settlement import publish_settled
from app.core.workflow.message_queue.session_message import (
    DecisionBody,
    NoticeBody,
    SessionMessage,
)
from app.core.workflow.node_helper.common import _message_context, _runtime_context, emit
from app.models.message_metadata import ToolMessageMetadata
from app.service import depends as service_depends
from app.storage.store_engines import main_db_factory

# 卡片短提示：等用户决定的调用没等到回答（区别于用户主动跳过，那是 ``completed``）。
UNANSWERED_HINT = "未作答"

def ends_on_user_input(tool_name: str) -> bool:
    """这个工具的等待决定是否等的是用户的回应：用户说了别的话就结束（``ToolDefinition.decision_lifetime``）。"""
    definition = get_tool_registry().get_tool_definition(tool_name)
    return definition is not None and definition.decision_lifetime == "until_user_input"


ExpiryCause = Literal["user_message", "run_stopped", "run_ended"]

_EXPIRY_REASONS: dict[ExpiryCause, str] = {
    "user_message": (
        "the user sent a new message instead. Treat that message as their response; do not re-ask."
    ),
    "run_stopped": (
        "the user stopped you instead of answering. Do not wait for an answer; re-ask only if the "
        "decision is still needed."
    ),
    "run_ended": (
        "your run ended before the user answered. Do not wait for an answer; re-ask only if the "
        "decision is still needed."
    ),
}


@dataclass(frozen=True)
class IncomingDecisionEffects:
    """这次待并入的消息会怎样改变「等用户决定」的调用；并入之前就要知道答案的地方共用。

    字段:
        user_input: 这批消息里的第一条用户输入（没有为 ``None``）；有它，等用户回应的等待就要作废。
        answered_before_input: 决定排在 ``user_input`` 前面的调用——先作答、后发消息，回答算数。
        answered: 这批消息里有决定的全部调用；并入之后它们都不再等待（被回答，或已作废而决定被丢弃）。
    """

    user_input: SessionMessage | None
    answered_before_input: frozenset[str]
    answered: frozenset[str]

    def still_awaiting(self, tool_call_id: str, tool_name: str) -> bool:
        """这次并入之后，这个此刻还在等待的调用是否仍在等。"""
        if tool_call_id in self.answered:
            return False
        return self.user_input is None or not ends_on_user_input(tool_name)


def incoming_decision_effects(pending: list[SessionMessage]) -> IncomingDecisionEffects:
    """按出队顺序读这批消息（``RunMailbox.ordered``）；决定与输入同优先级、按到达先后并入，所以
    先后有意义。

    只有用户自己的输入算「说了别的话」。别的 Agent 发来的消息（子 Agent 汇报、会话间消息）同样
    会开启回合，但与用户答不答无关：让它作废提问，卡片就会在用户没操作时消失。
    """

    def decided(messages: list[SessionMessage]) -> frozenset[str]:
        return frozenset(
            cast(DecisionBody, message.body).tool_call_id
            for message in messages
            if message.kind == "user_decision"
        )

    index = next((i for i, message in enumerate(pending) if message.kind == "user_input"), None)
    return IncomingDecisionEffects(
        user_input=None if index is None else pending[index],
        answered_before_input=decided(pending if index is None else pending[:index]),
        answered=decided(pending),
    )


def expired_notice(tool_call_id: str, cause: ExpiryCause) -> NoticeBody:
    """告诉模型它的一次提问没等到回答、为什么：等待占位说过「结果另行送达」，不说它会一直等。

    Run 内作废时经 ``mailbox.notify`` 与这批通知一起写入；Run 落定后作废时经外部队列投一次，随
    下一次驱动并入（不为它开新 Run）。
    """
    return NoticeBody(
        content=(
            f"Your question (tool_call_id={tool_call_id}) was not answered: "
            f"{_EXPIRY_REASONS[cause]}"
        ),
        notice_kind="decision_expired",
    )


def expire_decisions(*, step_id: str, keep: frozenset[str] = frozenset()) -> list[str]:
    """把上下文里仍在等待、等用户回应的决定作废，返回被作废的调用 id；工作线程执行。

    ``keep`` 里的调用不动：刚写下的那次（重复提问时不作废自己）、决定已排在新输入前面待并入的。
    一个事务改完全部占位，提交后逐个发卡片状态事件，「等你操作」的计数随之更新。之后迟到的决定
    在并入时发现占位已不在等待，只确认不写入。
    """
    rc = _runtime_context()
    context = _message_context()
    expired = [
        call_id
        for call_id, tool_name in context.awaiting_decision_calls()
        if call_id not in keep and ends_on_user_input(tool_name)
    ]
    if not expired:
        return []
    with main_db_factory().begin() as db:
        written = [
            context.update_tool_result_metadata(
                call_id, ToolMessageMetadata(status="cancelled", error=UNANSWERED_HINT), db=db
            )
            for call_id in expired
        ]
    for call_id, metadata in zip(expired, written, strict=True):
        emit(
            ToolCallStatusChangedEvent(
                session_id=rc.run.session_id,
                run_id=rc.run.id,
                step_id=step_id,
                tool_call_id=call_id,
                status=metadata.status,
                error=metadata.error,
                display_data=metadata.display_data,
            )
        )
    log.info(
        "decisions_expired",
        extra={
            "msg": "等用户回应的等待决定已作废",
            "data": {"run_id": rc.run.id, "step_id": step_id, "tool_call_ids": expired},
        },
    )
    return expired


def expire_decisions_after_run(session_id: int, hint: str) -> list[str]:
    """Run 被停下、失败或被重启收敛后，把这个 Session 仍在等待、等用户回应的决定作废，返回被
    作废的调用 id。

    这样结束的回合不会再往下走（恢复是同一个 Run 的新驱动，届时模型按通知决定要不要再问）。留着
    不作废，之后的回答会开出新 Run、落进新回合，被停下的 Run 还会先读到「用户已停止」再读到回答。
    正常完成不在此列，由调用方决定（见 ``SessionActor._expire_decisions``）。``hint`` 说明被
    什么结束（已停止 / 未作答 / 已中断）。

    Run 内的作废见 :func:`expire_decisions`（要同时改内存工作副本）；这里只在没有驱动在跑时
    调用，直接改库。恰在此刻提交的回答会开出一个新 Run，并入时发现占位已不在等待而被丢弃——少见，接受。
    是否告知模型由调用方决定。同步写库，调用方放在工作线程。
    """
    context = service_depends.get_session_context_service()
    expiring = frozenset(
        call_id
        for call_id, (tool_name, _arguments) in context.awaiting_decision_calls(session_id).items()
        if ends_on_user_input(tool_name)
    )
    if not expiring:
        return []
    settled = context.expire_awaiting_decisions(session_id, expiring, hint)
    publish_settled(settled, "cancelled", hint)
    expired = [str(record.message.get("tool_call_id")) for record in settled]
    log.info(
        "decisions_expired_after_run",
        extra={
            "msg": "Run 落定，等用户回应的等待决定已作废",
            "data": {"session_id": session_id, "tool_call_ids": expired, "hint": hint},
        },
    )
    return expired
