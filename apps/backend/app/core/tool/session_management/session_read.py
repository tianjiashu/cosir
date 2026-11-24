"""session_read 工具：读取同工作区任意一个 Session 的进度——状态、此刻在做什么、最近几步与最终输出。"""

from typing import Any

from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_SESSION_READ,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.tool.session_management.addressing import find_workspace_session
from app.core.tool.session_management.args.session_read_args import SessionReadArgs
from app.core.tool.session_management.progress import SessionProgress
from app.core.tool.tool_base import HandlerBase


class SessionReadTool(HandlerBase):
    """只读、不写任何事实；进度的推导归 ``SessionProgress``，本工具只负责寻址与包装结果。"""

    name: str = TOOL_SESSION_READ
    # 中文译文：读取这个工作区里任意一个会话的进度（你派生的、它们再派生的，以及侧栏里的其他会话）：状态、此刻
    # 在做什么、还没并入的排队消息、最近几步（说了什么、调了哪些工具、结果如何），落定后还有最终输出。
    # 用它决定是等、追加指示还是停掉，而不是发「做完了吗」。不要反复调用来等它做完：你直接派生的会话落定时
    # 会汇报给你，用 wait_messages 等；其他会话不会汇报给你。进度是线索：关键结论仍以汇报和你自己的核对为准。
    description: str = (
        "Read the progress of any session in this workspace (sessions you derived, the ones they "
        "derived, and the other conversations listed by session_list): its status, what it is doing "
        "now, queued messages it has not taken in, its recent steps (what it said, which tools it "
        "called and how they ended), and its final output once finished. Use it to decide whether to "
        "wait, send more instructions or stop it, instead of asking 'are you done?'. Do not call it "
        "repeatedly to wait for it to finish: a session you derived directly reports to you when its "
        "run ends, so use wait_messages; other sessions do not report to you. Progress is a lead, not "
        "proof: rely on reports and your own checks for key conclusions."
    )
    args_model: type[SessionReadArgs] = SessionReadArgs
    group = TOOL_GROUP_CHILD_AGENT

    @staticmethod
    def _display_data(progress: SessionProgress) -> dict[str, Any]:
        """UI-only 结果：对方 Session 的定位、名字与落定后的最终输出；状态由前端读会话状态推送，不镜像。"""
        return {
            "kind": "agent-session-result",
            "operation": "read",
            "session_id": progress.session_id,
            "agent_name": progress.name,
            "final_output": progress.final_output,
            "end_reason": progress.end_reason,
        }

    def execute(
        self, session_id: int, steps: int, execution_context: ToolExecutionContext
    ) -> ToolObservation:
        session = find_workspace_session(self.name, session_id, execution_context)
        if isinstance(session, ToolObservation):
            return session
        progress = SessionProgress.read(
            session, steps=steps, reader_id=execution_context.session_id
        )
        return tool_success(
            tool_name=self.name,
            content=progress.render(),
            display_data=self._display_data(progress),
        )

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            deferred=True,
        )


def build_session_read_definition() -> ToolDefinition:
    """供工具注册表登记的 session_read 定义。"""
    return SessionReadTool().to_definition()
