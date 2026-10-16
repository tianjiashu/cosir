"""运行时系统通知的值对象；不依赖模型消息角色或 Transport 协议。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


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

    def to_hint_text(self) -> str:
        """供模型节点消费时追加到消息 content 的通知文本。

        不构造独立消息：历史上曾用 ``ToolMessage`` 承载，因缺必填 ``tool_call_id`` 在
        langchain 校验器里直接 ``KeyError`` 崩溃。通知以 ``system_hint:`` 前缀拼接进本步
        请求最后一条消息的 content 末尾，仅作用于当次模型请求；路由信息（``kind`` /
        ``run_id``）由消费边界据此判断，不在此处落地为消息字段。
        """
        return self.content
