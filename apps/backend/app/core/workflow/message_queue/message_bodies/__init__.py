"""任务消息协议：每类消息的领域正文，以及它如何变成一条带标签的 context 消息。

所有消息进模型时都是 user 角色，来源由 ``TaskMessage.kind`` 表达（由 body 类型决定，持久化时按
它还原 body），后端规则只读 ``kind``、不解析正文。何时并入只看 ``delivery``，是否投影只看
``project_to_transport``，投递方不做额外判断；两者的默认值由 body 类型决定，生产者可覆盖。能否
推进（开新 Run、唤醒挂起的 Run）也是类型规则（``WAKES``），由 ``dispatch_policy`` 读取。

每类消息渲染为一个标签（``TAG``）：属性与正文只放结构化字段，不附带说明文字；读法由类型自己
声明（``PROMPT_GUIDE``），``SystemPromptBuilder`` 遍历 :data:`MODEL_INPUT_BODIES` 生成系统提示词
里的消息协议并只写一次。标签名与读法和 render 同处一个类，改消息格式时就地同步，提示词无需另改。
外部来源文本（用户反馈、子 Agent / 后台任务输出）里出现的同名标签先转义，防止伪造边界。

新增一种消息：在本包加一个 body 模块（入队的继承 :class:`EnqueuedMessageBody`；``ToolResultBody``
这类只写 context、不入队的不继承），再登记进本文件的 :data:`MessageBody`、:data:`MODEL_INPUT_BODIES`
与 ``_BODY_OF_KIND``——三者与登记处同处一文件，避免只改其中一半。
"""

from app.core.workflow.message_queue.message_bodies.background_result import BackgroundResultBody
from app.core.workflow.message_queue.message_bodies.base import EnqueuedMessageBody, TaskMessageKind
from app.core.workflow.message_queue.message_bodies.decision import DecisionBody
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody, notices_to_context
from app.core.workflow.message_queue.message_bodies.rendering import escape_tag, render_time
from app.core.workflow.message_queue.message_bodies.tool_result import ToolResultBody
from app.core.workflow.message_queue.message_bodies.user_input import RunLaunch, UserInputBody

__all__ = [
    "MODEL_INPUT_BODIES",
    "BackgroundResultBody",
    "DecisionBody",
    "EnqueuedMessageBody",
    "MessageBody",
    "NoticeBody",
    "RunLaunch",
    "TaskMessageKind",
    "ToolResultBody",
    "UserInputBody",
    "escape_tag",
    "notices_to_context",
    "render_time",
]

# 入队消息的联合类型；``ToolResultBody`` 不在其中（它只在 Run 内直接写 context）。
MessageBody = UserInputBody | NoticeBody | DecisionBody | BackgroundResultBody

# 进模型的全部带标签 user 消息类型，顺序即系统提示词里消息协议的列出顺序；新增类型须加入。
MODEL_INPUT_BODIES: tuple[type[EnqueuedMessageBody | ToolResultBody], ...] = (
    UserInputBody,
    NoticeBody,
    DecisionBody,
    ToolResultBody,
    BackgroundResultBody,
)

# 持久化读回：按 ``kind`` 列还原 body。与上面两个清单同处一文件，新增类型三处一起改。
_BODY_OF_KIND: dict[str, type[MessageBody]] = {
    body.KIND: body for body in (UserInputBody, NoticeBody, DecisionBody, BackgroundResultBody)
}
