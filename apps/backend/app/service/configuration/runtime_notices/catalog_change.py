"""能力目录变更的运行时通知：子 Agent、Skill、MCP 服务三种目录共用。

这三种目录在 Task 首次装配时写进系统提示词并固化（``SystemPromptBuilder._build_tool_catalog``），
之后配置再变也不改写提示词；已在本进程装配过、并且固化了对应工具的 Task 改为收到一条 Task 级
通知，下一次推进时写入上下文。本模块不读写配置，由各配置服务在写入成功后发布。

通知只带目录本来就给模型看的字段（名称与描述），不带系统提示词、模型配置、连接参数或凭据；
描述按 JSON 字面量编码，用户输入里的换行不会破坏通知结构。

作用域：``scope`` 为 ``None`` 表示系统级变更，命中所有工作区；否则只命中该工作区。Skill、MCP 的
workspace 同名项覆盖系统项，所以系统级条目变了、而某个工作区有同名覆盖时，那个工作区
看到的目录没变，不能通知它——由发布方经 ``shadowed_in(工作区根, 条目名)`` 回答「这个工作区是否
覆盖了它」。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfileType
from app.core.tool.schemas.tool_names import TOOL_DELEGATE_TASK, TOOL_MCP, TOOL_SKILL
from app.core.workflow.message_queue.message_bodies.rendering import escape_tag
from app.core.workflow.message_queue.task_message import NoticeBody
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.utils.workspace_scope import normalize_scope_path

if TYPE_CHECKING:
    from app.task_runtime.task_runtime_space import TaskRuntimeSpace

CatalogName = Literal["agents", "skills", "mcp_servers"]
CatalogChangeAction = Literal["added", "updated", "removed"]

# 目录 → 让 Task 看得到这份目录的工具：没固化这个工具的 Task 提示词里本来就没有这份目录。
_CATALOG_TOOLS: dict[CatalogName, str] = {
    "agents": TOOL_DELEGATE_TASK,
    "skills": TOOL_SKILL,
    "mcp_servers": TOOL_MCP,
}
_CATALOG_LABELS: dict[CatalogName, str] = {
    "agents": "可委派子 Agent",
    "skills": "Skill",
    "mcp_servers": "MCP 服务",
}


@dataclass(frozen=True)
class CatalogChange:
    """一个目录里一个条目的一次可见变化。

    ``shadowed_in(workspace_root, entry_id)`` 只对系统级变更有意义：返回 ``True`` 表示该工作区有
    同名条目覆盖它，那里的 Task 不通知。子 Agent 不允许 workspace 覆盖系统级，发布方不传。
    """

    catalog: CatalogName
    scope: str | None
    action: CatalogChangeAction
    entry_id: str
    previous_description: str | None = None
    current_description: str | None = None
    shadowed_in: Callable[[str, str], bool] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def between(
        cls,
        catalog: CatalogName,
        *,
        scope: str | None,
        before: Mapping[str, str | None],
        after: Mapping[str, str | None],
        shadowed_in: Callable[[str, str], bool] | None = None,
    ) -> list[CatalogChange]:
        """比较一份目录变更前后的「条目 → 描述」，得到逐条变化（按条目名排序）。

        发布方传入的应是 Agent 看到的视图：workspace 变更传该工作区的合并视图（删掉覆盖后回落到
        系统项，就是 ``updated`` 而不是 ``removed``），系统级变更传系统层本身。
        """

        changes: list[CatalogChange] = []
        for entry_id in sorted(set(before) | set(after)):
            if entry_id not in before:
                action: CatalogChangeAction = "added"
            elif entry_id not in after:
                action = "removed"
            elif before[entry_id] != after[entry_id]:
                action = "updated"
            else:
                continue
            changes.append(
                cls(
                    catalog=catalog,
                    scope=scope,
                    action=action,
                    entry_id=entry_id,
                    previous_description=before.get(entry_id),
                    current_description=after.get(entry_id),
                    shadowed_in=shadowed_in,
                )
            )
        return changes

    def to_notice(self) -> NoticeBody:
        """转换为 Task 级系统通知。"""

        scope = "system" if self.scope is None else self.scope
        # 描述来自配置（workspace 的 Skill 描述可能随仓库而来），JSON 编码不转义尖括号，再转义两层
        # 外框标签，描述里写出 ``</system_notice>`` 也不能伪造通知边界。
        def literal(value: str | None) -> str:
            text = json.dumps(value, ensure_ascii=False)
            return escape_tag(escape_tag(text, "catalog_update"), NoticeBody.TAG)

        content = "\n".join(
            (
                "<catalog_update>",
                f"{_CATALOG_LABELS[self.catalog]}目录已变更，请在下一次使用前应用以下事实：",
                f"目录：{self.catalog}",
                f"作用域：{literal(scope)}",
                f"动作：{self.action}",
                f"名称：{literal(self.entry_id)}",
                f"变更前描述：{literal(self.previous_description)}",
                f"变更后描述：{literal(self.current_description)}",
                "未列出的目录信息保持不变。",
                "</catalog_update>",
            )
        )
        return NoticeBody(
            content=content,
            notice_kind="catalog_change",
            metadata=self._log_data(),
        )

    def reaches(self, space: TaskRuntimeSpace) -> bool:
        """这个 Task 是否看得到本次变化：已装配、不是隐藏 Agent、固化了对应工具、在作用域内且没被覆盖。

        只读 space 记下的 Agent 与工作区（理由见 ``SystemPromptDelta.reaches``），不为判断创建上下文。
        """

        agent = space.agent
        if agent is None or agent.agent_type is AgentProfileType.HIDDEN:
            return False
        if _CATALOG_TOOLS[self.catalog] not in space.task_tool_names:
            return False
        workspace_root = space.current_workspace.root_path
        if self.scope is None:
            return self.shadowed_in is None or not self.shadowed_in(workspace_root, self.entry_id)
        return normalize_scope_path(workspace_root) == normalize_scope_path(self.scope)

    def broadcast(self) -> int:
        """投给命中的已物化 Task，返回投递数。

        配置已经写入，通知是旁路：投递失败只记日志并返回 0，不让配置请求失败。
        """

        try:
            delivered = task_runtime_spaces.broadcast_notice(self.to_notice(), self.reaches)
        except Exception:
            log.exception(
                "catalog_change_broadcast_failed",
                extra={"msg": "配置已保存，但目录变更通知投递失败", "data": self._log_data()},
            )
            return 0
        log.info(
            "catalog_change_broadcast",
            extra={
                "msg": "目录变更已广播到相关 Task runtime space",
                "data": {**self._log_data(), "delivered_count": delivered},
            },
        )
        return delivered

    def _log_data(self) -> dict[str, str | None]:
        return {
            "catalog": self.catalog,
            "scope": self.scope,
            "action": self.action,
            "entry_id": self.entry_id,
        }


def broadcast_catalog_changes(changes: Iterable[CatalogChange]) -> int:
    """逐条广播，返回总投递数。"""

    return sum(change.broadcast() for change in changes)
