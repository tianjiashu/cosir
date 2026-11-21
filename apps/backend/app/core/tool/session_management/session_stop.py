"""session_stop 工具：停止一个自己派生的 Session，连带收回它交出去的工作。"""

from app.config.logging.logger import log
from app.core.tool.session_management.args.session_stop_args import SessionStopArgs
from app.core.tool.session_management.addressing import find_stoppable_session
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_SESSION_STOP,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.tool.tool_base import HandlerBase
from app.models.enums.run_status import ACTIVE_RUN_STATUSES
from app.service.depends import get_session_service
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces


class SessionStopTool(HandlerBase):
    """以上游停止（``parent``）停下派生 Session，与撤销委派、撤回回合同一条路（``SessionActor.cancel``）。

    为什么是 ``parent`` 而不是 ``user``：Agent 停掉自己派生的 Session，意思是「这份结果我不要了」，它的后台
    命令与下级 Session 一并收回（``SessionRecall``）；Run 以 ``parent_cancelled`` 落定，不汇报、不留通知，
    调用方自己知道停了它。

    只投递、不等待（与 HTTP 取消的 202 一致）：工具线程不能等对方的 Session 闸门，``request_cancel`` 交给
    actor 的事件循环。没有活动 Run 时什么也不做并明说，不假装停下了什么。活动判定与投递之间的竞争无害：
    actor 在闸门内重新判定，对已落定的 Run 取消是空操作。
    """

    name: str = TOOL_SESSION_STOP
    # 中文译文：停止你直接派生的一个会话：打断它当前的运行，并收回它启动的后台命令和它派生的会话（整棵下级一起
    # 停）。只在必要时用：它的结果你已经不需要了，或者它明显走偏、用 session_send 纠正也不管用。否则让它做完，或
    # 用 session_send 追加指示——停掉会丢掉它进行中的工作。更深一层的会话只有它的派生方能停。不会回报给你；停止前
    # 已排队的消息不会自动接着跑，之后再用 session_send 发消息会开出新的运行。用 session_read 确认已停下。
    description: str = (
        "Stop a session you derived directly: interrupt its current run and recall the background "
        "commands and sessions it started (everything below it stops too). Use it only when necessary: "
        "you no longer need its result, or it is clearly going the wrong way and correcting it with "
        "session_send will not help. Otherwise let it finish or send it instructions; stopping throws "
        "away its work in progress. Deeper sessions can be stopped only by their own deriver. It sends "
        "no report back. Messages queued before the stop do not run automatically; a later session_send "
        "starts a new run. Confirm with session_read."
    )
    args_model: type[SessionStopArgs] = SessionStopArgs
    group = TOOL_GROUP_CHILD_AGENT

    def execute(self, session_id: int, execution_context: ToolExecutionContext) -> ToolObservation:
        session = find_stoppable_session(self.name, session_id, execution_context)
        if isinstance(session, ToolObservation):
            return session
        run = get_session_service().require_current_run(session.id)
        display = {
            "kind": "agent-session-result",
            "operation": "stop",
            "session_id": session.id,
            "agent_name": session.title,
        }
        if run is None or run.status not in ACTIVE_RUN_STATUSES:
            return tool_success(
                tool_name=self.name,
                content=f"session {session.id} is idle; nothing to stop.",
                display_data=display,
            )
        session_runtime_spaces.get_or_create(session.id).actor.request_cancel("parent")
        log.info(
            "derived_session_stop_requested",
            extra={
                "msg": "Agent 请求停止自己派生的 Session",
                "data": {
                    "session_id": execution_context.session_id,
                    "run_id": execution_context.run_id,
                    "target_session_id": session.id,
                    "target_status": run.status,
                },
            },
        )
        return tool_success(
            tool_name=self.name,
            content=(
                f"stop requested for session {session.id} (was {run.status}); its background commands and "
                "derived sessions are recalled too. It will not report back."
            ),
            display_data=display,
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


def build_session_stop_definition() -> ToolDefinition:
    """供工具注册表登记的 session_stop 定义。"""
    return SessionStopTool().to_definition()
