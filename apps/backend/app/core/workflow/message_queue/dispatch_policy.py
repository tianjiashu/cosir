"""Task 消息调度规则：纯函数，不读写数据库、不触碰执行器，可逐条单测。

Task 的调度 actor（``TaskActor``）读出最新 Run 与队列，交给这里判定，再执行判定结果。规则只读
消息属性（``kind`` / ``delivery``）与 Task 的执行阶段，投递方不另加判断。

用户输入按 ``delivery`` 分两条通道：``immediate`` 是「现在就处理」（空闲时发送、排队消息被点了
立即发送），``next_run`` 是排队（活动 Run 期间发送）。一个新 Run 只消费**一条**用户输入（见
:func:`next_input`），排队消息因此一轮一条依次处理，各用各自发送时的配置；其余种类的消息不受
此限，新 Run 照常全部接收。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

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
      结果同样会开出新 Run。配置取这个 Run 将消费的那条用户输入（:func:`next_input`），没有用户
      输入时继承当前 Run；无从继承（模型配置已删除、尚无 Run）时不动，消息留在队列里。
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
    selected = next_input(pending)
    if selected is not None:
        return StartRun(cast(UserInputBody, selected.body).launch)
    launch = RunLaunch.from_run(current)
    return StartRun(launch) if launch is not None else Stay()


def next_input(pending: list[TaskMessage]) -> TaskMessage | None:
    """新 Run 要消费的那条用户输入：先取最早的 ``immediate``，没有再取最早的 ``next_run``。

    ``immediate`` 优先：取消后队列里留着旧的排队消息时，用户新发送（或点了立即发送）的那条先跑，
    旧排队消息等它完成后再依次接续。
    """
    inputs = [message for message in pending if isinstance(message.body, UserInputBody)]
    return next(
        (message for message in inputs if message.delivery == "immediate"),
        inputs[0] if inputs else None,
    )


def initial_messages(pending: list[TaskMessage], *, fresh_run: bool) -> list[TaskMessage]:
    """执行开始时投递的消息。

    新 Run 接收全部非用户输入的消息与 :func:`next_input` 选出的一条用户输入，其余用户输入留在
    队列里等后续 Run；唤醒 / 续跑只接收 ``immediate``。
    """
    if not fresh_run:
        return [message for message in pending if message.delivery == "immediate"]
    selected = next_input(pending)
    return [
        message for message in pending
        if not isinstance(message.body, UserInputBody) or message is selected
    ]


def matches_run(launch: RunLaunch, run: ConversationRunRecord) -> bool:
    """发起配置是否与该 Run 一致；生产者据此为运行中发送选择 ``delivery``。"""
    return (
        launch.agent_id == run.agent_id
        and launch.model_config_id == run.model_config_id
        and launch.options == run.options
    )
