"""session_list 工具：列出自己的派生树（委派子 Agent、Agent fork 及它们再派生的），每个一行带「此刻在做什么」。"""

from typing import cast

from app.core.tool.session_management.args.session_list_args import SessionListArgs
from app.core.tool.session_management.addressing import can_stop
from app.core.tool.session_management.progress import SessionProgress
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_SESSION_LIST,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.tool.tool_base import HandlerBase
from app.service.depends import get_session_service

_HEADER = (
    "Your derivation tree, each session indented under the one that derived it. [read, stop]: you derived "
    "it directly; [read]: only its own deriver can stop it."
)


class SessionListTool(HandlerBase):
    """列整棵派生树并逐行标出能做什么：读取认整棵树、停止只认直接一层，规则只在 ``addressing``。

    没有展示 ``kind``：结果不指向单个 Session，调用行只显示标题与状态。
    """

    name: str = TOOL_SESSION_LIST
    # 中文译文：列出你的派生树：你派生的会话（委派的子 Agent 与分叉）以及它们再派生的，每个缩进在派生它的会话
    # 之下，同层按创建先后，一行一个：id、名字、状态、运行了多久、此刻在做什么，以及你能对它做什么（read 全部
    # 都能读；stop 只有你直接派生的能停）。找某个会话的 id、或一眼看清全部进展时用；要看某一个最近几步用
    # session_read。不要反复调用来等它们做完：用 wait_messages。
    description: str = (
        "List your derivation tree: the sessions you derived (delegated child agents and forks) and "
        "the ones they derived, each indented under the session that derived it, siblings in creation "
        "order. One line each: id, name, status, how long it has run, what it is doing now, and what "
        "you may do with it (read: any of them; stop: only the ones you derived directly). Use it to "
        "find a session's id or to see all of them at a glance; use session_read for one session's "
        "recent steps. Do not call it repeatedly to wait for them to finish: use wait_messages."
    )
    args_model: type[SessionListArgs] = SessionListArgs
    group = TOOL_GROUP_CHILD_AGENT

    def execute(self, execution_context: ToolExecutionContext) -> ToolObservation:
        caller = execution_context.session_id
        tree = get_session_service().list_derivation_tree(caller)
        if not tree:
            return tool_success(tool_name=self.name, content="You have not derived any sessions.")
        # 先序遍历保证派生方先出现，层数按它的派生方推出。
        levels: dict[int, int] = {caller: -1}
        lines = [_HEADER]
        for session in tree:
            # 树里的 Session 都有派生方（树就是按派生关系走出来的）。
            deriver = cast(int, session.deriver_id())
            level = levels[deriver] + 1
            levels[session.id] = level
            line = SessionProgress.read(session, steps=0).summary_line(
                derived_by=deriver if level > 0 else None
            )
            marks = "[read, stop]" if can_stop(session, caller) else "[read]"
            lines.append(f"{'  ' * level}{line} {marks}")
        return tool_success(tool_name=self.name, content="\n".join(lines))

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            # 只在管理派生 Session 时用，延迟加载；与 session_read / session_stop 同名前缀，一次检索全部取到。
            deferred=True,
        )


def build_session_list_definition() -> ToolDefinition:
    """供工具注册表登记的 session_list 定义。"""
    return SessionListTool().to_definition()
