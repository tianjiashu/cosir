"""Agent 消息的来源与汇报：谁发来的、以什么身份（含派生方），随消息一起持久化与投影。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class AgentReport(BaseModel):
    """派生 Session 的一次汇报：它的某个 Run 以什么结局落定，落定时下游是否还有没了结的工作。

    由编排在 Run 落定时生成（``core.tool.multi_agent.derived_report``），不是 Agent 写的；``intermediate``
    为真表示这只是中间结果——它还有运行中的命令或活动的派生 Session，之后再落定时会再汇报一次。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["completed", "failed"]
    intermediate: bool


class MessageSource(BaseModel):
    """Agent 消息的发送方：去哪儿看它（``session_id``）、叫它什么（``name``，发送时的显示名快照），以及
    这条消息是不是它的汇报（``report``），以及它是不是收件方的派生方（``from_deriver``）。

    委派与追加消息的来源是父 Session，派生 Session 的汇报来源是它自己。名字是快照，来源改名或被删不回写
    已发出的消息。
    ``report`` 放在来源上而不是消息上：它说的是「谁以什么身份发来」，随来源一起持久化、投影，各条
    路径不必另带一个字段。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: int
    name: str
    report: AgentReport | None = None
    # 发送方是不是收件 Session 的派生方（``SessionRecord.derived_by``）：委派与 Agent fork 的任务说明、派生方
    # 后来追加的消息都是。只给模型看（渲染成 ``relation="parent"``，见 ``AgentMessageBody``）：其他 Agent
    # 的消息是待评估的信息，派生方的消息是这个 Session 的任务；不投影到前端。
    from_deriver: bool = False
