"""模型请求前的输入组装节点：并入 inbox 输入、写入运行期通知，再交给 model 节点。

inbox 的消费时机归这里（每次模型请求前取一批）：``TaskInputService.consume`` 一个事务领取并删出
inbox，本节点再逐条经 ``RuntimeMessageContextManager.add_message`` 写入 context 并发消费事件；全程
持 Task 操作闸门，与入队、编辑等同 Task 写入串行。并入前不必先闭合工具协议：``load_message``
发请求前会把被用户消息隔开的工具结果按调用恢复到合法顺序。
"""

import asyncio

from openai.types.chat import ChatCompletionSystemMessageParam

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.events import UserInputConsumedEvent
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.node_helper.common import (
    RunStopped,
    _message_context,
    _runtime_context,
    emit,
    route_state,
)
from app.core.workflow.node_helper.finalize_max_steps import _finalize_max_steps
from app.models.enums.conversation_run_status import RunTransition
from app.models.message_metadata import UserMessageMetadata
from app.service import depends as service_depends
from app.service.conversation_run.task_input_service import user_message


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
    # inbox 转移与投影都在 Task 闸门内完成：入队后的排队投影同样持闸门，消费事件因此总能在
    # snapshot 的排队消息里找到对应条目。
    async with rc.task_space.async_operation():
        inputs = await asyncio.to_thread(
            service_depends.get_task_input_service().consume,
            rc.task.id,
        )
        for message_id, user_input in inputs.items():
            _message_context().add_message(
                user_message(user_input),
                metadata=UserMessageMetadata(presentation=user_input.presentation()),
                message_id=message_id,
            )
            emit(UserInputConsumedEvent(task_id=rc.task.id, run_id=rc.run.id, message_id=message_id))
    # 运行期通知（tools / runner / 配置变更广播投递）在这里消费：归属过滤由
    # ``take_runtime_notices(run_id=...)`` 在出队时完成。取回的是压成一段的提示文本，作为独立
    # ``system 消息`` 落库进 canonical context——成为历史固定行后，后续 Run 的请求前缀才不会分叉。
    notice = rc.task_space.take_runtime_notices(run_id=rc.run.id)
    if notice:
        _message_context().add_message(
            ChatCompletionSystemMessageParam(role="system", content=notice)
        )
    return route_state(state.step_count, ReactRoute.MODEL)


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
