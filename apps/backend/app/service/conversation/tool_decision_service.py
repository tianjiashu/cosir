"""HITL 决定入队：对一次等待用户决定的工具调用提交决定的唯一入口。

调用方是用户经 HTTP 决定入口（``AssistantTransportService.decide``）：校验与入队放在 service 层，
而不是只属于 HTTP 用户路径的 Transport 编排里——工具层不依赖 ``assistant_transport``。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ValidationError

from app.config.configuration import get_tool_registry
from app.config.logging.logger import log
from app.core.tool.validation.call_arguments import CallArgumentsError, resolve_call_arguments
from app.core.workflow.message_queue.session_message import DecisionBody, SessionMessage
from app.models.errors.input_errors import UserInputRejectedError
from app.models.errors.session_command_errors import SessionCommandConflictError
from app.service import depends as service_depends
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces
from app.storage.store_engines import main_db_factory
from app.storage.write_transaction import begin_immediate


class ToolDecisionService:
    """校验并入队 ``DecisionBody``；不等工具执行，决定之后的去向由 ``ToolDefinition.user_decision`` 决定。"""

    def submit(
            self,
            session_id: int,
            tool_call_id: str,
            decision: Literal["approve", "reject"],
            *,
            feedback: str | None = None,
            data: dict[str, Any] | None = None,
    ) -> None:
        """对 ``tool_call_id`` 作出决定并入队；同步读库与写队列，异步调用方放进工作线程。

        只校验对所有 HITL 工具都成立、且能同步回答的事实：调用的等待占位仍为 ``pending``（事实源是
        context，不读工具自己的业务事实）、队列里没有它未处理的决定。工具自己的业务状态在执行决定时由工具判定。
        工具声明了载荷（``ToolDefinition.decision_model``）时，批准必须带合法载荷、驳回不带；这里是
        载荷的唯一校验点，给模型的回执也在这里由载荷渲染定稿（见 ``DecisionBody``）。

        判定与入队在同一个 ``BEGIN IMMEDIATE`` 事务里：同一调用的两次决定请求可能同时到达，
        分开读写会让两者都通过判定、各入队一条；写锁也让判定与 ``prepare_input``
        「确认决定 + 结算占位」的事务互斥，两次读取看到的是同一时刻的事实。

        异常:
            SessionCommandConflictError: ``DECISION_NOT_AWAITED`` / ``DECISION_ALREADY_SUBMITTED``。
            UserInputRejectedError: ``DECISION_DATA_INVALID``。
            KeyError: Session 不存在（入队时）。
        """
        context = service_depends.get_session_context_service()
        queue = service_depends.get_session_message_queue()
        with begin_immediate(main_db_factory()) as db:
            call = context.awaiting_decision_calls(session_id, db=db).get(tool_call_id)
            if call is None:
                raise SessionCommandConflictError("DECISION_NOT_AWAITED", "该操作已不在等待决定，请刷新后重试")
            if any(
                    isinstance(message.body, DecisionBody) and message.body.tool_call_id == tool_call_id
                    for message in queue.pending_nowait(session_id, db=db)
            ):
                raise SessionCommandConflictError("DECISION_ALREADY_SUBMITTED", "该决定已提交，正在处理")
            tool_name, arguments = call
            payload, reply = _validated_payload(tool_name, arguments, decision, data)
            body = DecisionBody(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                decision=decision,
                feedback=feedback.strip() if feedback and feedback.strip() else None,
                data=payload,
                reply=reply,
            )
            session_runtime_spaces.enqueue(session_id, SessionMessage.of(body), db=db)
        log.info(
            "tool_decision_enqueued",
            extra={
                "msg": "决定已入队，等待工具执行或交给模型",
                "data": {
                    "session_id": session_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "decision": decision,
                },
            },
        )

    def awaits_user_reply(self, session_id: int) -> bool:
        """Session 是否有决定在等用户回应（提问）；发送路径据此决定消息是否唤醒挂起的 Run。

        同步读库，异步调用方放进工作线程。
        """
        calls = service_depends.get_session_context_service().awaiting_decision_calls(session_id)
        definitions = (get_tool_registry().get_tool_definition(name) for name, _ in calls.values())
        return any(
            definition is not None and definition.decision_lifetime == "until_user_input"
            for definition in definitions
        )


def _validated_payload(
        tool_name: str,
        arguments: str,
        decision: Literal["approve", "reject"],
        data: dict[str, Any] | None,
) -> tuple[dict[str, object] | None, str | None]:
    """按工具声明校验载荷，返回「入队的载荷、给模型的回执」；工具没声明载荷时两者都是 ``None``。

    原调用的业务参数作为校验上下文交给载荷模型：所选项是否属于这次调用，只有它知道。业务参数经
    ``resolve_call_arguments`` 取得，与当初 handler 收到的是同一份（原始参数里还夹着 ``intent``，
    类型也可能没归一，不能直接用）。原调用当初过了门禁，这里解析不了只可能是工具的参数声明在提问
    挂着期间变了：记日志并拒绝，不放行一份没法核对的回答。驳回不带载荷，回执用工具声明的固定文案。
    """
    definition = get_tool_registry().get_tool_definition(tool_name)
    model = definition.decision_model if definition is not None else None
    if definition is None or model is None or decision == "reject":
        if data is not None:
            raise UserInputRejectedError("DECISION_DATA_INVALID", "这个决定不能携带回答内容")
        return None, model.DECLINED_REPLY if model is not None else None
    try:
        asked = resolve_call_arguments(definition, arguments).arguments
    except CallArgumentsError as exc:
        log.error(
            "tool_decision_call_unreadable",
            extra={
                "msg": "等待决定的原调用参数已不符合工具当前的参数声明，无法核对回答",
                "data": {"tool_name": tool_name, "error": str(exc)},
            },
        )
        raise UserInputRejectedError(
            "DECISION_DATA_INVALID", "这次提问已失效，无法作答；请直接发消息说明", detail=str(exc)
        ) from exc
    try:
        payload = model.model_validate(data or {}, context={"args": asked})
    except ValidationError as exc:
        raise UserInputRejectedError(
            "DECISION_DATA_INVALID", "回答内容与这次提问不符，请刷新后重试", detail=str(exc)
        ) from exc
    return payload.model_dump(mode="json"), payload.reply()
