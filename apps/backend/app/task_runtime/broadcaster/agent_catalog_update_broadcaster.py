"""子 Agent 目录变更的进程内延迟广播。

本模块只负责把 ``AgentCatalogChange`` 投递给当前进程中真正能看到委派目录的 Agent。消息
通过共享的延迟系统消息广播器进入 TaskRuntimeSpace，不创建新任务、不终止已有 Run，也不
修改 Agent Registry。
"""

from __future__ import annotations

from app.core.agents.agent_profile import AgentProfileType
from app.core.tools.schemas.tool_names import TOOL_DELEGATE_TASK
from app.task_runtime.agent_catalog_change import AgentCatalogChange
from app.task_runtime.broadcaster.deferred_system_message_broadcaster import (
    broadcast_deferred_system_message,
)
from app.task_runtime.task_runtime_space import TaskRuntimeSpace
from app.utils.workspace_scope import normalize_scope_path


def broadcast_agent_catalog_change(change: AgentCatalogChange) -> int:
    """把子 Agent 目录变更延迟投递给相关的委派 Agent。"""

    return broadcast_deferred_system_message(
        message=change.to_message(),
        should_notify=lambda space: _should_notify_space(space, change),
        event="agent_catalog_change_broadcast",
        log_message="子 Agent 目录变更已广播到相关 Task runtime space",
        data={
            "action": change.action,
            "agent_id": change.agent_id,
            "scope": change.scope,
        },
    )


def _should_notify_space(space: TaskRuntimeSpace, change: AgentCatalogChange) -> bool:
    """判断一个 runtime space 是否能看到本次目录变更。"""

    manager = space.get_context_manager()
    if manager is None:
        return False
    if manager.agent_profile.agent_type is AgentProfileType.HIDDEN:
        return False
    if TOOL_DELEGATE_TASK not in manager.agent_profile.allowed_tools:
        return False
    if change.scope is None:
        return True
    return normalize_scope_path(manager.workspace_root) == normalize_scope_path(change.scope)
