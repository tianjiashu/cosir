"""子 Agent 目录变更的延迟系统通知值对象。

本模块只描述配置中心向相关 Agent 发送的目录变更消息，不负责判断通知范围、修改注册表或
持久化消息。描述字段按 JSON 字面量编码，避免用户输入中的换行破坏通知结构；消息仍然只
承载目录所需的 Agent ID 和描述，不携带系统提示词、模型配置或凭据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import SystemMessage

AGENT_CATALOG_CHANGE_NOTICE_TYPE = "agent_catalog_change"
AgentCatalogChangeAction = Literal["created", "updated", "deleted"]


@dataclass(frozen=True)
class AgentCatalogChange:
    """描述一次会影响委派目录的子 Agent 配置变更。

    参数:
        scope: ``None`` 表示系统级目录，否则为 workspace 根路径。
        action: 新增、修改描述或删除。
        agent_id: 发生变化的子 Agent 标识。
        previous_description: 变更前描述；新增时为 ``None``。
        current_description: 变更后描述；删除时为 ``None``。

    返回:
        ``to_message`` 返回下一次模型节点消费的临时 ``SystemMessage``。

    异常:
        无。字段由配置 service 在写入成功后构造，消息编码不会执行文件或网络 IO。

    副作用:
        无。本对象不写入数据库、context 或 runtime queue。
    """

    scope: str | None
    action: AgentCatalogChangeAction
    agent_id: str
    previous_description: str | None
    current_description: str | None

    def to_message(self) -> SystemMessage:
        """把目录变更转换成模型可见的延迟系统消息。"""

        scope = "system" if self.scope is None else self.scope
        content = "\n".join(
            (
                "<agent_catalog_update>",
                "可委派子 Agent 目录已变更，请在下一次委派前应用以下事实：",
                f"作用域：{json.dumps(scope, ensure_ascii=False)}",
                f"动作：{self.action}",
                f"agent_id：{json.dumps(self.agent_id, ensure_ascii=False)}",
                f"变更前描述：{json.dumps(self.previous_description, ensure_ascii=False)}",
                f"变更后描述：{json.dumps(self.current_description, ensure_ascii=False)}",
                "未列出的 Agent 目录信息保持不变。",
                "</agent_catalog_update>",
            )
        )
        return SystemMessage(
            content=content,
            additional_kwargs={
                "notice_type": AGENT_CATALOG_CHANGE_NOTICE_TYPE,
                "action": self.action,
                "agent_id": self.agent_id,
                "scope": self.scope,
            },
        )
