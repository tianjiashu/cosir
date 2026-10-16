"""运行时系统通知的值对象；不依赖模型消息角色或 Transport 协议。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class RuntimeNotice:
    """供下一次模型步骤消费的系统通知，可安全广播到多个 Task。

    ``run_id=None`` 表示 Task 级通知，否则仅所属 Run 可消费。正文保持原始文本、不添加任何前缀
    （``system_hint:`` 声明由消费边界统一加一次，见 :meth:`to_hint_text`）。消费边界
    ``TaskRuntimeSpace.take_runtime_notices`` 负责把多条通知拼成一段文本，模型节点再把它落为
    ``SystemMessage``——本对象自身不做 Message 映射，队列本身也不负责持久化。metadata 仅含标量
    来源信息，构造时复制并冻结，避免广播后被生产方或其他 Task 修改。
    """

    content: str
    kind: str
    run_id: int | None = None
    metadata: Mapping[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_hint_text(self) -> str:
        """返回供模型消费的正文，原样回吐 ``content``、不加任何包装。

        ``system_hint:`` 标记、宿主平台声明与逐行拼接都归消费边界
        （``TaskRuntimeSpace.take_runtime_notices``）统一收口，本方法只回吐正文——这样同一条通知
        广播到多个 Task 时不会各自累加前缀。``kind`` / ``run_id`` 只服务队列的归属过滤与观测，
        不落成消息字段（历史上曾把通知映射为 ``ToolMessage``，因缺必填 ``tool_call_id`` 在
        langchain 校验器里直接 ``KeyError`` 崩溃，故不再走「每条通知一个消息」的路子）。
        """
        return self.content
