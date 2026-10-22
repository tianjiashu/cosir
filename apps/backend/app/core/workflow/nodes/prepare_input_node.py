"""模型请求前的输入组装节点：闭合工具协议、并入 inbox 输入、写入运行期通知，再交给 model 节点。

inbox 的消费时机归这里（每次模型请求前取一批），原子转移归 ``TaskInputService.consume``，序号与
内存列表归 ``RuntimeMessageContextManager``；三者在 Task 操作闸门内交接，与入队、编辑等同 Task
写入串行。
"""

import asyncio

from openai.types.chat import ChatCompletionSystemMessageParam

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.context.context_entry import ContextEntry
from app.core.events import UserInputConsumedEvent
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.execution_state.runtime_context import RuntimeContext
from app.core.workflow.node_helper.common import (
    _message_context,
    _runtime_context,
    emit,
    route_state, RunStopped,
)
from app.core.workflow.node_helper.finalize_max_steps import _finalize_max_steps
from app.models.enums.conversation_run_status import RunTransition
from app.models.message_metadata import UserMessageMetadata
from app.service import depends as service_depends


async def _prepare_input_node(state: ReactExecutionState) -> dict[str, object]:
    """组装本步模型输入后流转到 model 节点。

    取消或步数已到上限时不并入 inbox，直接交给 model 节点落定：排队消息留给后续 Run，不被一个
    不会再请求模型的 Run 吞掉。
    """
    rc = _runtime_context()
    step_count = state.step_count + 1
    step_id = f"step-{step_count}"
    if rc.is_cancelled():
        _cancel_before_request(step_id, rc.run.id)
    # 超配额时不再发起推理，落定 max_steps_reached 失败终态。
    if step_count > rc.step_limit:
        return await _finalize_max_steps(step_count)
    entries = await asyncio.to_thread(_consume_inbox, rc)
    for entry in entries:
        metadata = entry.metadata
        if not isinstance(metadata, UserMessageMetadata) or metadata.presentation is None:
            raise ValueError("已消费用户输入缺少展示绑定")
        emit(
            UserInputConsumedEvent(
                task_id=rc.task.id,
                run_id=rc.run.id,
                message_id=entry.message_id,
                presentation=metadata.presentation,
            )
        )
    # 运行期通知（tools / runner / 配置变更广播投递）在这里消费：归属过滤由
    # ``take_runtime_notices(run_id=...)`` 在出队时完成。取回的是压成一段的提示文本，作为独立
    # ``system 消息`` 落库进 canonical context——成为历史固定行后，后续 Run 的请求前缀才不会分叉。
    notice = rc.task_space.take_runtime_notices(run_id=rc.run.id)
    if notice:
        _message_context().add_message(
            ChatCompletionSystemMessageParam(role="system", content=notice)
        )
    return route_state(state.step_count, ReactRoute.MODEL)


def _consume_inbox(rc: RuntimeContext) -> list[ContextEntry]:
    """在 Task 闸门内把当前批次输入转入 context，并交给 manager 接纳；工作线程执行（含 IO）。"""
    context = _message_context()
    # 先闭合协议：并入的用户消息不能插进未闭合的工具调用与其结果之间。
    context.close_unclosed_tool_calls()
    with rc.task_space.operation():
        entries = service_depends.get_task_input_service().consume(
            rc.task.id, rc.run.id, rc.run.model_config_id, context.next_sequence
        )
        context.accept_committed(entries)
    return entries



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