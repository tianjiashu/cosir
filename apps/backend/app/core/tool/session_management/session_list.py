"""session_list 工具：列出这个工作区里的会话——自己的派生树（委派子 Agent、Agent fork 及它们再派生的）与侧栏里的
其他会话，每个一行带「此刻在做什么」。"""

from typing import cast

from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_SESSION_LIST,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.tool.session_management.addressing import can_stop
from app.core.tool.session_management.args.session_list_args import SessionListArgs
from app.core.tool.session_management.progress import SessionProgress
from app.core.tool.tool_base import HandlerBase
from app.models.session.session_record import SessionRecord
from app.service.depends import get_session_service

_TREE_HEADER = (
    "Your derivation tree, each session indented under the one that derived it. [read, stop]: you derived "
    "it directly; [read]: only its own deriver can stop it."
)
_OTHERS_HEADER = (
    "Other conversations in this workspace, most recently active first. [read]: you can read it and "
    "message it with session_send, but not stop it."
)
# 第二段的条数上限：每条都要读一次进度（几次本机查询），历史会话可能上百；按最近活动取前几条，够找到
# 正在协作的会话，更早的只报总数。
_OTHERS_LIMIT = 20


class SessionListTool(HandlerBase):
    """分两段列出：自己的整棵派生树，与侧栏里的其他会话（不展开它们的派生树）；能做什么的规则只在
    ``addressing``。

    第二段只取侧栏那一层：那是用户心里「这个工作区有哪些对话」，别人的子 Agent 要看时读它的派生方即可，
    「此刻」一行会写它在等哪个派生会话。没有展示 ``kind``：结果不指向单个 Session，调用行只显示标题与状态。
    """

    name: str = TOOL_SESSION_LIST
    # 中文译文：列出这个工作区里的会话，分两段。第一段是你的派生树：你派生的会话（委派的子 Agent 与分叉）以及
    # 它们再派生的，每个缩进在派生它的会话之下，同层按创建先后；read 全部都能读，stop 只有你直接派生的能停。
    # 第二段是工作区里的其他对话（侧栏里的会话，最近活动的在前，最多 20 个），能读、能用 session_send 发消息，
    # 不能停。一行一个：id、名字、状态、运行了多久、此刻在做什么。用户正在直接对话的会话会标出来，那次运行是
    # 用户的。找某个会话的 id、或一眼看清全部进展时用；要看某一个最近几步用 session_read。不要反复调用来等它们
    # 做完：用 wait_messages。
    description: str = (
        "List the sessions in this workspace, in two parts. First your derivation tree: the sessions "
        "you derived (delegated child agents and forks) and the ones they derived, each indented under "
        "the session that derived it, siblings in creation order; you may read any of them and stop "
        "only the ones you derived directly. Then the other conversations in this workspace (the "
        "user's sidebar, most recently active first, at most 20), which you may read and message with "
        "session_send but not stop. One line each: id, name, status, how long it has run and what it "
        "is doing now. Sessions the user is talking to directly are marked; that run is the user's. "
        "Use it to find a session's id or to see all of them at a glance; use session_read for one "
        "session's recent steps. Do not call it repeatedly to wait for them to finish: use "
        "wait_messages."
    )
    args_model: type[SessionListArgs] = SessionListArgs
    group = TOOL_GROUP_CHILD_AGENT

    def execute(self, execution_context: ToolExecutionContext) -> ToolObservation:
        caller = get_session_service().get_session(execution_context.session_id)
        tree = get_session_service().list_derivation_tree(caller.id)
        others = self._other_conversations(caller, tree, execution_context.workspace_id)
        if not tree and not others:
            return tool_success(
                tool_name=self.name, content="There are no other sessions in this workspace."
            )
        lines = self._tree_lines(caller.id, tree)
        if others:
            lines.extend(["", *self._other_lines(caller, others)])
        return tool_success(tool_name=self.name, content="\n".join(lines))

    @staticmethod
    def _tree_lines(caller_id: int, tree: list[SessionRecord]) -> list[str]:
        if not tree:
            return ["You have not derived any sessions."]
        # 先序遍历保证派生方先出现，层数按它的派生方推出。
        levels: dict[int, int] = {caller_id: -1}
        lines = [_TREE_HEADER]
        for session in tree:
            # 树里的 Session 都有派生方（树就是按派生关系走出来的）。
            deriver = cast(int, session.deriver_id())
            level = levels[deriver] + 1
            levels[session.id] = level
            line = SessionProgress.read(session, steps=0, reader_id=caller_id).summary_line(
                derived_by=deriver if level > 0 else None
            )
            marks = "[read, stop]" if can_stop(session, caller_id) else "[read]"
            lines.append(f"{'  ' * level}{line} {marks}")
        return lines

    @staticmethod
    def _other_conversations(
        caller: SessionRecord, tree: list[SessionRecord], workspace_id: int
    ) -> list[SessionRecord]:
        """侧栏会话里除去自己、已在派生树里的（自己分出的 Agent fork）与尚未开始的新会话页。"""
        excluded = {caller.id, *(session.id for session in tree)}
        return [
            session
            for session in get_session_service().list_sessions_for_workspace(workspace_id)
            if session.id not in excluded and not session.unstarted
        ]

    @staticmethod
    def _other_lines(caller: SessionRecord, others: list[SessionRecord]) -> list[str]:
        lines = [_OTHERS_HEADER]
        for session in others[:_OTHERS_LIMIT]:
            line = SessionProgress.read(session, steps=0, reader_id=caller.id).summary_line()
            if session.id == caller.deriver_id():
                line = f"{line}; it derived you"
            lines.append(f"{line} [read]")
        hidden = len(others) - _OTHERS_LIMIT
        if hidden > 0:
            lines.append(f"{hidden} older conversation(s) not shown.")
        return lines

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
