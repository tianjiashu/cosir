"""Task 消息调度规则：纯函数，不读写数据库、不触碰执行器，可逐条单测。

Task 的调度 actor（``TaskActor``）读出最新 Run 与队列，交给这里判定，再执行判定结果。规则只读
消息属性（``kind`` / ``delivery``）与 Run 状态，投递方不另加判断。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import ConversationRunRecord
from app.models.enums.conversation_run_status import ACTIVE_RUN_STATUSES, ConversationRunStatus
from app.core.workflow.inbox.task_message import (
    RunLaunch,
    TaskMessage,
    UserInputBody,
)


@dataclass(frozen=True)
class Deliver:
    """把这些消息投进当前执行的 mailbox。"""

    messages: list[TaskMessage]


@dataclass(frozen=True)
class Wake:
    """挂起的 Run 继续：``suspended → running``，同一个 Run 从 prepare_input 进入。"""


@dataclass(frozen=True)
class StartRun:
    """以该配置新建 Run 并启动执行。"""

    launch: RunLaunch


@dataclass(frozen=True)
class Stay:
    """不动。"""


Action = Deliver | Wake | StartRun | Stay


def next_action(
    current: ConversationRunRecord | None,
    has_executor: bool,
    pending: list[TaskMessage],
    fresh: list[TaskMessage],
) -> Action:
    """推进规则的唯一实现：有消息就处理，系统通知例外。

    ``pending`` 是外部队列里全部未确认的消息，``fresh`` 是其中这次新到达、尚未判定过的部分（由
    actor 的水位区分）。只看新到达的消息决定要不要推进：残留消息（运行中排队的 ``next_run``、取消
    或失败后没处理的）不会反复触发，随下一次推进一起并入。

    - 有执行者：投递全部 ``immediate`` 消息，``next_run`` 留给下一个 Run；mailbox 按行 id 去重。
    - 挂起：新到达的 ``immediate`` 消息里有能推进的（``WAKES``，系统通知除外）就唤醒同一个 Run。
    - 终态或尚无 Run：新到达的消息里有能推进的就新开 Run，不区分谁发起——用户停止后到达的后台
      结果同样会开出新 Run。配置取队列中**最后**一条用户输入（用户最新的选择），没有用户输入时
      继承当前 Run；无从继承（模型配置已删除、尚无 Run）时不动，消息留在队列里。
    """
    if has_executor:
        return Deliver([message for message in pending if message.delivery == "immediate"])
    status = current.status if current is not None else None
    if status == ConversationRunStatus.SUSPENDED:
        wakes = any(
            message.delivery == "immediate" and type(message.body).WAKES for message in fresh
        )
        return Wake() if wakes else Stay()
    if status in ACTIVE_RUN_STATUSES:
        # running 却没有执行者：只在启动瞬间或进程状态异常时出现，不在这里另开执行。
        return Stay()
    if not any(type(message.body).WAKES for message in fresh):
        return Stay()
    launches = [
        message.body.launch for message in pending if isinstance(message.body, UserInputBody)
    ]
    launch = launches[-1] if launches else _launch_of(current)
    return StartRun(launch) if launch is not None else Stay()


def initial_messages(pending: list[TaskMessage], *, fresh_run: bool) -> list[TaskMessage]:
    """执行开始时投递的消息：新 Run 接收全部剩余消息；唤醒 / 续跑只接收 ``immediate``。"""
    if fresh_run:
        return pending
    return [message for message in pending if message.delivery == "immediate"]


def _launch_of(run: ConversationRunRecord | None) -> RunLaunch | None:
    """沿用一个 Run 的发起配置；模型配置已被删除时无法继承。"""
    if run is None or run.agent_id is None or run.model_config_id is None:
        return None
    return RunLaunch(
        agent_id=run.agent_id, model_config_id=run.model_config_id, options=run.options
    )


def matches_run(launch: RunLaunch, run: ConversationRunRecord) -> bool:
    """发起配置是否与该 Run 一致；生产者据此为运行中发送选择 ``delivery``。"""
    return (
        launch.agent_id == run.agent_id
        and launch.model_config_id == run.model_config_id
        and launch.options == run.options
    )
