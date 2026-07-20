"""运行时使用的与模型无关的消息协议。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuntimeMessage:
    """表示一个与模型无关的运行时消息。

    参数:
        role: 消息角色，例如 ``system``、``user``、``assistant`` 或 ``tool``。
        content_text: 纯文本消息内容。
        metadata: 用于追踪和后续扩展的可选结构化元数据。

    返回:
        一个运行时消息值对象。

    异常:
        无。

    副作用:
        无。
    """

    role: str
    content_text: str
    metadata: dict[str, str] = field(default_factory=dict)
