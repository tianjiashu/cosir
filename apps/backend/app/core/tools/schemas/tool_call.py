"""Tool call value object."""

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages.tool import ToolCall as LangChainToolCall

from app.core.tools.schemas.user_decision import UserDecision


@dataclass(frozen=True)
class ToolCall:
    """A model-requested tool invocation.

    ``user_decision`` 是框架级（非模型可见）的附加字段：当某个调用在 human-in-the-loop 流程中
    被用户批准后，``tools`` 节点以同一 ``call_id`` 重新发起该调用并带上用户决定，handler 据此
    走真正的执行分支。它**不进** ``arguments``——参数是模型可见可写的，把「已获批准」放进参数
    等于把审批绕过口开在模型协议层。
    """

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    user_decision: UserDecision | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCall":
        """从内部工具调用 dict 重建 ``ToolCall`` 值对象。

        约定源 dict 使用内部字段名（``tool_name`` / ``arguments`` / ``call_id``）。
        对缺失字段做安全兜底，保证任意调用路径（审批恢复、取消占位等）构造出的
        ``ToolCall`` 字段口径完全一致，避免多处分支手写字段导致漂移。注意：本工厂
        服务于内部 dict，LangChain 原始结构（``name`` / ``args`` / ``id``）的转换由
        ``langchain_bridge.tool_calls_from_langchain`` 单独负责，二者不共用。

        参数:
            data: 含工具调用字段的内部 dict。

        返回:
            字段经兜底补全后的 ``ToolCall`` 实例。

        异常:
            无（字段缺失时按默认值兜底，不抛出）。

        副作用:
            无。
        """
        return cls(
            tool_name=data.get("tool_name", ""),
            arguments=data.get("arguments") or {},
            call_id=data.get("call_id") or "",
            user_decision=data.get("user_decision") or None,
        )

    @classmethod
    def from_from_langchain(cls, data: LangChainToolCall) -> "ToolCall":
        """从 LangChain ``tool_calls`` 结构重建 ``ToolCall`` 值对象。

        与 :meth:`from_dict` 对应：源 dict 使用 LangChain 字段名（``name`` / ``args`` /
        ``id``），供 ``langchain_bridge.tool_calls_from_langchain`` 把模型产出的
        ``tool_calls`` 还原为内部值对象。字段缺失时按默认值兜底。

        参数:
            data: 含 LangChain 字段名的工具调用 dict（``name`` / ``args`` / ``id``）。

        返回:
            字段经兜底补全后的 ``ToolCall`` 实例。

        异常:
            无（字段缺失时按默认值兜底，不抛出）。

        副作用:
            无。
        """
        return cls(
            tool_name=data.get("name", ""),
            arguments=data.get("args", {}) or {},
            call_id=str(data.get("id") or ""),
        )
