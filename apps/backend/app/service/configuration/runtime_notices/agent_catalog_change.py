"""子 Agent 目录变更的运行时通知。

委派目录在 Task 的系统提示词里固化；子 Agent 新增、删除或改了描述时，能委派的 Task 收到一条
Task 级通知。通知只带目录需要的 Agent ID 与描述，不带系统提示词、模型配置或凭据；描述按 JSON
字面量编码，用户输入里的换行不会破坏通知结构。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfileType
from app.core.tool.schemas.tool_names import TOOL_DELEGATE_TASK
from app.core.workflow.message_queue.task_message import NoticeBody
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.utils.workspace_scope import normalize_scope_path

if TYPE_CHECKING:
    from app.task_runtime.task_runtime_space import TaskRuntimeSpace

AgentCatalogChangeAction = Literal["created", "updated", "deleted"]


@dataclass(frozen=True)
class AgentCatalogChange:
    """一次影响委派目录的子 Agent 配置变更。

    ``scope`` 为 ``None`` 表示系统级目录，否则为 workspace 根路径；新增时没有变更前描述，删除时没有
    变更后描述。
    """

    scope: str | None
    action: AgentCatalogChangeAction
    agent_id: str
    previous_description: str | None
    current_description: str | None

    def to_notice(self) -> NoticeBody:
        """转换为 Task 级系统通知。"""

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
        return NoticeBody(
            content=content,
            notice_kind="agent_catalog_change",
            metadata={"action": self.action, "agent_id": self.agent_id, "scope": self.scope},
        )

    def reaches(self, space: TaskRuntimeSpace) -> bool:
        """这个 Task 是否看得到本次目录变更：能委派、不是隐藏 Agent、在变更的作用域内。

        只读 space 记下的 Agent 与工作区，理由见 ``SystemPromptDelta.reaches``。
        """

        agent = space.agent
        if agent is None or agent.agent_type is AgentProfileType.HIDDEN:
            return False
        if TOOL_DELEGATE_TASK not in space.task_tool_names:
            return False
        if self.scope is None:
            return True
        workspace_root = normalize_scope_path(space.current_workspace.root_path)
        return workspace_root == normalize_scope_path(self.scope)

    def broadcast(self) -> int:
        """投给命中的已物化 Task，返回投递数。

        配置已经写入，通知是旁路：投递失败只记日志并返回 0，不让配置请求失败。
        """

        data = {"action": self.action, "agent_id": self.agent_id, "scope": self.scope}
        try:
            delivered = task_runtime_spaces.broadcast_notice(self.to_notice(), self.reaches)
        except Exception:
            log.exception(
                "agent_catalog_change_broadcast_failed",
                extra={"msg": "Agent 配置已保存，但目录变更通知投递失败", "data": data},
            )
            return 0
        log.info(
            "agent_catalog_change_broadcast",
            extra={
                "msg": "子 Agent 目录变更已广播到相关 Task runtime space",
                "data": {**data, "delivered_count": delivered},
            },
        )
        return delivered
