"""HITL 决定入队：对一次等待用户决定的工具调用提交决定的唯一入口。

调用方有两类：用户经 HTTP 决定入口（``TransportAssistantService.decide``），以及工具运行时在用户已
无法作出决定时代为落定（如交接中的终端会话结束，见 ``terminal_runtime.session_effects``）。两者走同一套
校验与入队，所以放在 service 层，而不是只属于 HTTP 用户路径的 Transport 编排里——工具层不依赖
``assistant_transport``。
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import ValidationError

from app.config.configuration import get_tool_registry
from app.config.logging.logger import log
from app.core.task_actor import SessionConflictError
from app.core.tool.schemas import ToolDefinition
from app.core.workflow.message_queue.task_message import DecisionBody, TaskMessage
from app.models.errors.input_errors import UserInputRejectedError
from app.service import depends as service_depends
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class ToolDecisionService:
    """校验并入队 ``DecisionBody``；不等工具执行，决定之后的去向由 ``ToolDefinition.user_decision`` 决定。"""

    def submit(
            self,
            task_id: int,
            tool_call_id: str,
            decision: Literal["approve", "reject"],
            *,
            feedback: str | None = None,
            data: dict[str, Any] | None = None,
            source: Literal["user", "runtime"] = "user",
    ) -> None:
        """对 ``tool_call_id`` 作出决定并入队；同步读库与写队列，异步调用方放进工作线程。

        只校验对所有 HITL 工具都成立、且能同步回答的事实：调用的等待占位仍为 ``pending``（事实源是
        context，不读工具自己的业务事实）、队列里没有它未处理的决定、批准载荷符合工具声明的
        ``decision_model``。工具自己的业务状态在执行决定时由工具判定。``source`` 只进日志，区分用户
        决定与运行时代为落定。

        异常:
            SessionConflictError: ``DECISION_NOT_AWAITED`` / ``DECISION_ALREADY_SUBMITTED``。
            UserInputRejectedError: ``DECISION_DATA_INVALID``。
            KeyError: Task 不存在（入队时）。
        """
        context = service_depends.get_conversation_task_context_service()
        queue = service_depends.get_task_message_queue()
        tool_name = context.awaiting_decision_tool(task_id, tool_call_id)
        if tool_name is None:
            raise SessionConflictError("DECISION_NOT_AWAITED", "该操作已不在等待决定，请刷新后重试")
        if any(
                isinstance(message.body, DecisionBody) and message.body.tool_call_id == tool_call_id
                for message in queue.pending_nowait(task_id)
        ):
            raise SessionConflictError("DECISION_ALREADY_SUBMITTED", "该决定已提交，正在处理")
        # 等待占位只会由已注册的 HITL 工具写入，定义必然存在。
        definition = cast(ToolDefinition, get_tool_registry().get_tool_definition(tool_name))
        payload = None
        if decision == "approve" and definition.decision_model is not None:
            try:
                payload = definition.decision_model.model_validate(data or {}).model_dump(mode="json")
            except ValidationError as exc:
                raise UserInputRejectedError(
                    "DECISION_DATA_INVALID", "提交的内容不合法，请检查后重试", detail=str(exc)
                ) from exc
        body = DecisionBody(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            decision=decision,
            feedback=feedback.strip() if feedback and feedback.strip() else None,
            data=payload,
        )
        task_runtime_spaces.enqueue(task_id, TaskMessage.of(body))
        log.info(
            "tool_decision_enqueued",
            extra={
                "msg": "决定已入队，等待工具执行或交给模型",
                "data": {
                    "task_id": task_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "decision": decision,
                    "source": source,
                },
            },
        )
