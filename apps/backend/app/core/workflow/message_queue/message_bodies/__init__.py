"""任务消息协议：每类消息的领域正文，以及它如何变成一条带标签的 context 消息。

所有消息进模型时都是 user 角色，来源由 ``TaskMessage.kind`` 表达（由 body 类型决定，持久化时按
它还原 body），后端规则只读 ``kind``、不解析正文。何时并入只看 ``delivery``，是否投影只看
``project_to_transport``，投递方不做额外判断；两者的默认值由 body 类型决定，生产者可覆盖。能否
推进（开新 Run、唤醒挂起的 Run）由消息体声明（``WAKE`` / ``wake_scope``），由 ``dispatch_policy`` 读取。

每类消息渲染为一个标签（``TAG``）：属性与正文只放结构化字段，不附带说明文字。系统提示词的消息
协议只写模型从消息本身推不出来的东西：通用规则（带 ``tool_call_id`` 的是那次调用的后续、只有用户
自己的话是指令）写在前言，各类型的读法（``PROMPT_GUIDE``）只写行为规则与正文里的语法，靠属性就能
说明自己的类型写 ``None``；``SystemPromptBuilder`` 遍历 :data:`MODEL_INPUT_BODIES` 生成并只写
一次。标签名与读法和 render 同处一个类，改消息格式时就地同步，提示词无需另改。
外部来源文本（用户反馈、子 Agent / 后台任务输出）里出现的同名标签先转义，防止伪造边界。

新增一种消息：在本包加一个 body 模块（入队的继承 :class:`EnqueuedMessageBody`；``ToolResultBody``、
``ToolImageBody`` 这类由节点直接写 context、不入队的不继承），再登记进本文件的 :data:`MessageBody`、
:data:`MODEL_INPUT_BODIES` 与 ``_BODY_OF_KIND``——三者与登记处同处一文件，避免只改其中一半。
"""

from app.core.workflow.message_queue.message_bodies.agent_message import AgentMessageBody
from app.core.workflow.message_queue.message_bodies.background_result import BackgroundResultBody
from app.core.workflow.message_queue.message_bodies.base import (
    EnqueuedMessageBody,
    TaskMessageKind,
    WakeScope,
)
from app.core.workflow.message_queue.message_bodies.decision import DecisionBody
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody, notices_to_context
from app.core.workflow.message_queue.message_bodies.rendering import escape_tag, render_time
from app.core.workflow.message_queue.message_bodies.tool_image import ToolImageBody
from app.core.workflow.message_queue.message_bodies.tool_result import ToolResultBody
from app.core.workflow.message_queue.message_bodies.user_input import RunLaunch, UserInputBody

__all__ = [
    "ADVANCING_KINDS",
    "LAUNCH_INPUT_BODIES",
    "MODEL_INPUT_BODIES",
    "AgentMessageBody",
    "BackgroundResultBody",
    "DecisionBody",
    "EnqueuedMessageBody",
    "LaunchInputBody",
    "MessageBody",
    "NoticeBody",
    "RunLaunch",
    "TaskMessageKind",
    "ToolImageBody",
    "ToolResultBody",
    "UserInputBody",
    "WakeScope",
    "escape_tag",
    "notices_to_context",
    "render_time",
]

# 入队消息的联合类型；``ToolResultBody`` / ``ToolImageBody`` 不在其中（节点直接写 context）。
MessageBody = (
    UserInputBody | AgentMessageBody | NoticeBody | DecisionBody | BackgroundResultBody
)

# 能交接新 Run 的输入：携带发起配置，一个 Run 只消费一条（见 ``dispatch_policy.next_input``）。
LaunchInputBody = UserInputBody | AgentMessageBody
LAUNCH_INPUT_BODIES: tuple[type[LaunchInputBody], ...] = (UserInputBody, AgentMessageBody)

# 进模型的全部带标签 user 消息类型，顺序即系统提示词里消息协议的列出顺序；新增类型须加入。
MODEL_INPUT_BODIES: tuple[type[EnqueuedMessageBody | ToolResultBody | ToolImageBody], ...] = (
    UserInputBody,
    AgentMessageBody,
    NoticeBody,
    DecisionBody,
    ToolResultBody,
    ToolImageBody,
    BackgroundResultBody,
)

# 持久化读回：按 ``kind`` 列还原 body。与上面两个清单同处一文件，新增类型三处一起改。
_BODY_OF_KIND: dict[str, type[MessageBody]] = {
    body.KIND: body
    for body in (UserInputBody, AgentMessageBody, NoticeBody, DecisionBody, BackgroundResultBody)
}

# 类型层面能推进 Run 的 kind（``WAKE`` 不是 ``none``）：外部队列里只有这些算「有待处理消息」。启动
# 调度据此判断是否要拉起 Task 的 actor。通知不在其中：只唤醒活动 Run 的那几种，所在 Task 若有挂起
# 的 Run，启动调度本来就会为它拉起 actor。
ADVANCING_KINDS: frozenset[TaskMessageKind] = frozenset(
    body.KIND for body in _BODY_OF_KIND.values() if body.WAKE != "none"
)
