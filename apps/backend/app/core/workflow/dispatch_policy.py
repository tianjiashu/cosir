"""Session 推进判定：给定执行阶段、当前 Run 与待处理消息，决定这次推进做什么（:class:`Action`）。

Session 的调度 actor（``SessionActor``）读出最新 Run 与队列，交给 :meth:`Action.decide` 判定，再执行
判定结果；结果只描述动作，执行（投递、迁移、建 Run）留在 actor。规则只读消息属性（``kind`` /
``delivery`` / 唤醒范围）、Session 的执行阶段与现有模型，投递方不另加判断；从一批消息里挑哪些、选哪条归
``PendingMessages``。纯规则，不读写数据库、不触碰执行器，可逐条单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from app.core.workflow.message_queue.pending_messages import PendingMessages
from app.core.workflow.message_queue.session_message import (
    LaunchInputBody,
    RunLaunch,
    SessionMessage,
)
from app.models.run.run_record import RunRecord

Phase = Literal["driving", "suspended", "idle"]
"""Session 的执行阶段：调度、取消、恢复与静止只按它判定，不各自组合 Run 状态与执行登记。

由 ``SessionActor`` 依当前 Run 与执行登记得出，本模块只消费：

- ``driving``：有执行者。以执行登记为准而不看 Run 状态：workflow 先写终态或挂起，执行者还要跑完
  STOP Hook、收束工具调用才释放，这段时间仍算 ``driving``——此时另开执行会出现两个执行者。
- ``suspended``：没有执行者且 Run 挂起，等可唤醒消息。
- ``idle``：没有执行者且 Run 已到终态，或尚无 Run。
"""


@dataclass(frozen=True)
class Action:
    """一次推进要做的事；具体是哪件看子类，由 :meth:`decide` 选出。"""

    @classmethod
    def decide(
        cls,
        phase: Phase,
        current: RunRecord | None,
        pending: PendingMessages,
        fresh: list[SessionMessage],
        available: frozenset[str],
    ) -> Action:
        """推进规则的唯一实现：有消息就处理，系统通知例外。

        ``pending`` 是外部队列里全部未确认的消息，``fresh`` 是其中这次新到达、尚未判定过的部分（由
        actor 的水位区分）。只看新到达的消息决定要不要推进：残留消息（运行中排队的 ``next_run``、取消
        或失败后没处理的）不会反复触发，随下一次推进一起并入。

        ``current`` 决定可唤醒哪个 Run，并为新 Run 提供继承的发起配置。``available`` 是模型配置文件里现有的
        模型 id（actor 每次推进读一次）：模型在配置文件里、Run 与消息只存 id，模型被删后配置仍指向原 id。

        - 有执行者：投递全部 ``immediate`` 消息，``next_run`` 留给下一个 Run；mailbox 按行 id 去重。
        - 挂起：新到达的 ``immediate`` 消息里有能推进活动 Run 的（唤醒范围不是 ``none``，见
          ``WakeScope``）就唤醒同一个 Run。
        - 终态或尚无 Run：新到达的消息里有能开新 Run 的（``new_or_active``）就新开 Run，不区分谁发起——
          用户停止不结束后台工作，之后到达的正常结果同样会开出新 Run；只对活动 Run 起作用的（单项取消的
          回执、派生工作被停止、重启通知）不会。配置取这个 Run 将消费的那条输入
          （``PendingMessages.next_input``，只在模型还在的输入里挑）；有输入但模型都已删除时不动，没有
          输入时继承当前 Run，无从继承（尚无 Run、模型已删除）时同样不动。不动时消息留在队列里，开不了
          的模型记在 ``unavailable`` 上交给 actor 记日志。
        """
        if phase == "driving":
            return Deliver(pending.immediate())
        if phase == "suspended":
            run_id = current.id if current else None
            wakes = any(m.delivery == "immediate" and m.wakes(run_id) for m in fresh)
            return Wake() if wakes else Stay()
        if not any(message.wakes(None) for message in fresh):
            return Stay()
        unavailable = tuple(
            cast(LaunchInputBody, message.body).launch.model_config_id
            for message in pending.stranded(available)
        )
        selected = pending.next_input(available)
        if selected is not None:
            return StartRun(cast(LaunchInputBody, selected.body).launch, selected, unavailable)
        if unavailable:
            return Stay(unavailable)
        launch = RunLaunch.from_run(current)
        if launch is None:
            return Stay()
        if launch.model_config_id not in available:
            return Stay((launch.model_config_id,))
        return StartRun(launch, None)


@dataclass(frozen=True)
class Deliver(Action):
    """把这些消息投进当前执行的 mailbox。"""

    messages: list[SessionMessage]


@dataclass(frozen=True)
class Wake(Action):
    """挂起的 Run 继续：``suspended → running``，同一个 Run 从 prepare_input 进入。"""


@dataclass(frozen=True)
class StartRun(Action):
    """以该配置新建 Run 并启动执行；``input`` 是它消费的那条输入（继承配置开 Run 时为 ``None``）。"""

    launch: RunLaunch
    input: SessionMessage | None
    unavailable: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stay(Action):
    """不动；``unavailable`` 是因模型已删除而开不了 Run 的配置。"""

    unavailable: tuple[str, ...] = ()
