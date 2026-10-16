"""运行时系统通知的值对象；不依赖模型消息角色或 Transport 协议。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from langchain_core.messages import AIMessage, BaseMessage


@dataclass(frozen=True)
class RuntimeNotice:
    """供下一次模型步骤消费的系统通知，可安全广播到多个 Task。

    ``run_id=None`` 表示 Task 级通知，否则仅所属 Run 可消费。正文保持原始文本，
    不添加消息前缀；具体 Message 映射由模型消费边界决定。metadata 仅含标量来源信息，
    构造时复制并冻结，避免广播后被生产方或其他 Task 修改。队列本身不负责持久化。
    """

    content: str
    kind: str
    run_id: int | None = None
    metadata: Mapping[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def build_runtime_notice_message(self) -> BaseMessage:
        """沿用现有 AIMessage 承载方式，后续消息角色选型只在此处调整。

        system_hint 前缀只在生成新消息时添加，不回写通知。此映射不等于已解决 thinking
        provider 的 reasoning_content 要求，也不伪造思考内容；该限制留待消息策略调整。
        持久化和是否投影到前端仍由模型节点负责。
        """
        metadata: dict[str, object] = dict(self.metadata)
        metadata["notice_type"] = self.kind
        if self.run_id is not None:
            metadata["run_id"] = self.run_id
        return AIMessage(content="system_hint:" + self.content, additional_kwargs=metadata)
