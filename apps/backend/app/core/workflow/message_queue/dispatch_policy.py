"""Task 消息调度规则：纯函数，不读写数据库、不触碰执行器，可逐条单测。

Task 的调度 actor（``TaskActor``）读出最新 Run 与队列，交给这里判定，再执行判定结果。规则只读
消息属性（``kind`` / ``delivery``）与 Task 的执行阶段，投递方不另加判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.workflow.message_queue.task_message import (
    RunLaunch,
    TaskMessage,
    UserInputBody,
)
from app.models import ConversationRunRecord

Phase = Literal["driving", "suspended", "idle"]
"""Task 的执行阶段：调度、取消、恢复与静止只按它判定，不各自组合 Run 状态与执行登记。

由 ``TaskActor`` 依当前 Run 与执行登记得出，本模块只消费：

- ``driving``：有执行者。以执行登记为准而不看 Run 状态：workflow 先写终态或挂起，执行者还要跑完
  STOP Hook、收束工具调用才释放，这段时间仍算 ``driving``——此时另开执行会出现两个执行者。
- ``suspended``：没有执行者且 Run 挂起，等可唤醒消息。
- ``idle``：没有执行者且 Run 已到终态，或尚无 Run。
"""


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
    phase: Phase,
    current: ConversationRunRecord | None,
    pending: list[TaskMessage],
    fresh: list[TaskMessage],
) -> Action:
    """推进规则的唯一实现：有消息就处理，系统通知例外。

    ``pending`` 是外部队列里全部未确认的消息，``fresh`` 是其中这次新到达、尚未判定过的部分（由
    actor 的水位区分）。只看新到达的消息决定要不要推进：残留消息（运行中排队的 ``next_run``、取消
    或失败后没处理的）不会反复触发，随下一次推进一起并入。

    ``current`` 只用来继承发起配置。

    - 有执行者：投递全部 ``immediate`` 消息，``next_run`` 留给下一个 Run；mailbox 按行 id 去重。
    - 挂起：新到达的 ``immediate`` 消息里有能推进的（``WAKES``，系统通知除外）就唤醒同一个 Run。
    - 终态或尚无 Run：新到达的消息里有能推进的就新开 Run，不区分谁发起——用户停止后到达的后台
      结果同样会开出新 Run。配置取队列中**最后**一条用户输入（用户最新的选择），没有用户输入时
      继承当前 Run；无从继承（模型配置已删除、尚无 Run）时不动，消息留在队列里。
    """
    if phase == "driving":
        return Deliver([message for message in pending if message.delivery == "immediate"])
    if phase == "suspended":
        wakes = any(
            message.delivery == "immediate" and type(message.body).WAKES for message in fresh
        )
        return Wake() if wakes else Stay()
    if not any(type(message.body).WAKES for message in fresh):
        return Stay()
    inputs = [message.body for message in pending if isinstance(message.body, UserInputBody)]
    if inputs:
        return StartRun(inputs[-1].launch)
    launch = RunLaunch.from_run(current)
    return StartRun(launch) if launch is not None else Stay()


def initial_messages(pending: list[TaskMessage], *, fresh_run: bool) -> list[TaskMessage]:
    """执行开始时投递的消息：新 Run 接收全部剩余消息；唤醒 / 续跑只接收 ``immediate``。"""
    if fresh_run:
        return pending
    return [message for message in pending if message.delivery == "immediate"]


def matches_run(launch: RunLaunch, run: ConversationRunRecord) -> bool:
    """发起配置是否与该 Run 一致；生产者据此为运行中发送选择 ``delivery``。"""
    return (
        launch.agent_id == run.agent_id
        and launch.model_config_id == run.model_config_id
        and launch.options == run.options
    )
