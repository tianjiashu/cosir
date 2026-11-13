"""用户消息的展示绑定：未消费时归 message_queue，消费后归 context metadata，不在 Run 留副本。"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def image_asset_id(path: str) -> str:
    """从已发布图片的 workspace 相对路径推导资源 id（文件名去掉扩展名）。

    附件存储固定为 ``<asset_id>.<ext>``。展示投影必须用同一规则推导 id，否则同一份展示绑定在
    「能不能渲染」上会得到两种结论。
    """
    return Path(path).name.split(".", 1)[0]


class AgentReport(BaseModel):
    """派生 Task 的一次汇报：它的某个 Run 以什么结局落定，落定时下游是否还有没了结的工作。

    由编排在 Run 落定时生成（``core.tool.multi_agent.derived_report``），不是 Agent 写的；``intermediate``
    为真表示这只是中间结果——它还有运行中的命令或活动的派生 Task，之后再落定时会再汇报一次。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["completed", "failed"]
    intermediate: bool


class MessageSource(BaseModel):
    """Agent 消息的发送方：去哪儿看它（``task_id``）、叫它什么（``name``，发送时的显示名快照），以及
    这条消息是不是它的汇报（``report``）。

    委派与追加消息的来源是父 Task，派生 Task 的汇报来源是它自己。名字是快照，来源改名或被删不回写
    已发出的消息。
    ``report`` 放在来源上而不是消息上：它说的是「谁以什么身份发来」，随来源一起持久化、投影，各条
    路径不必另带一个字段。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: int
    name: str
    report: AgentReport | None = None


class UserInputPresentation(BaseModel):
    """随 user 消息持久化的展示绑定；不重复保存 SDK 消息正文或 Run 执行配置。

    正文与模型看到的是同一份规范化胶囊文本（``<file>`` / ``<ref>`` / ``<skill>`` 指针，Skill
    已补上 ``path``），不再分开保存两份正文。``image_paths`` 是已上传图片的 workspace 相对路径，
    顺序即发送时的图片顺序。

    「胶囊指针可用」由入队边界 ``UserInputService`` 保证：那里 stat 本机文件、校验工作区引用、
    解析 Skill 并写回路径。本模型只承载已成立的事实，不再重复校验自身——它在每次快照重建与
    context 读取时都会被 ``model_validate``，为上游已保证的数据反复付出代价没有收益。
    """

    model_config = ConfigDict(extra="forbid")
    text: str
    image_paths: list[str] = Field(default_factory=list)
