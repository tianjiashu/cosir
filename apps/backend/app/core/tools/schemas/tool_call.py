"""工具执行入口的调用值对象。"""

from dataclasses import dataclass, field
from typing import Any

from app.models.user_decision import UserDecision


@dataclass(frozen=True)
class ToolCall:
    """已解析参数的工具调用。

    ``user_decision`` 是框架级（非模型可见）的附加字段：当某个调用在 human-in-the-loop 流程中
    被用户批准后，``tools`` 节点以同一 ``call_id`` 重新发起该调用并带上用户决定，handler 据此
    走真正的执行分支。它**不进** ``arguments``——参数是模型可见可写的，把「已获批准」放进参数
    等于把审批绕过口开在模型协议层。
    """

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    user_decision: UserDecision | None = None
