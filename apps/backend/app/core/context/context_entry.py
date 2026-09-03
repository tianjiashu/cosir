"""运行时上下文条目值对象。"""

from dataclasses import dataclass

from app.models import RuntimeMessage


@dataclass(frozen=True)
class ContextEntry:
    """承载消息及其在 task 上下文中的生命周期归属。

    内存中的条目即「进模型的上下文」：只有会进入模型上下文的消息才被持有，
    是否进模型的标记由持久化层 ``TurnMessageModel.in_context`` 承载，不在此
    值对象上冗余。

    参数:
        message: 模型无关的运行时消息。
        run_id: 消息所属 turn；task 级消息使用 ``None``。

    返回:
        不可变的上下文条目。

    异常:
        无。

    副作用:
        无。
    """

    message: RuntimeMessage
    run_id: int | None
