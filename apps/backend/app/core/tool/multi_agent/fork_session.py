"""fork_session 工具：把当前会话复制成一条侧栏可见的分叉会话，交给它一份子会话，当场完成。

与 ``delegate_task`` 的分工：委派换一个子 Agent 身份、只带任务说明；fork 不换身份，带着到上一条回复为止的
上下文去做。两者共用派生的其余部分：派生深度（``core.tool.derivation.child_depth``）、以 Agent 消息
投递任务说明、Run 落定时由编排向本 Session 汇报（``derived_report``），父 Agent 用 ``wait_messages``
等汇报。用户在分叉里直接对话开出的 Run 不汇报。

继承的历史是第一人称的，分叉自己分不清哪些事归它：曾把来源没做完的请求接过去，并自认是父会话。
所以任务说明前先投一条交接通知，写清身份与会话归属；通知不进请求前缀，不影响缓存。

分叉是侧栏会话（``session_type="agent_fork"``，来源记在 ``extra.fork``），不挂在
所有权子树上：删除父对话时按用户 fork 的规则改挂，而不是一起删掉。父 Session 被上游收回时，分叉按
派生关系一并收回（``SessionRecall``）；用户停止父 Session 不停分叉。
"""

import json
from typing import Any, ClassVar, cast

from app.config.logging.logger import log
from app.core.permission.writable_root import WritableRootError, narrow
from app.core.tool.multi_agent.agent_message import prepare_agent_message
from app.core.tool.multi_agent.args.fork_session_args import ForkSessionArgs
from app.core.tool.multi_agent.derivation import child_depth
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_FORK_SESSION,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry
from app.core.workflow.message_queue.session_message import NoticeBody, RunLaunch, SessionMessage
from app.models.conversation_input import MessageSource
from app.models.errors.session_fork_errors import SessionForkConflictError
from app.service.depends import get_run_ledger, get_session_service
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces

_HANDOVER_NOTICE = (
    "You are session {fork_id}, a fork of session {source_id}. The conversation above was copied from "
    "that session as background: requests in it belong to that session, even where they read as your "
    "own work, so do not pick them up. Your only job is the assignment in the next message. Each "
    "time you finish working on it, your final reply is reported to session {source_id} "
    "automatically, so do not send it with send_message. If the user later talks to you here, "
    "follow the user; what you do for the user is not reported."
)
# 分叉沿用来源的系统提示词（接缓存），可写根只能在这里告知；来源自己也收窄过时以这一条为准。
_WRITABLE_ROOT_NOTICE = (
    " Your writable root is {writable_root} (relative to the workspace root): file-writing tools "
    "are refused outside it and commands start there by default. This replaces any writable "
    "area stated earlier. Do not change files outside it by other means; report what needs to "
    "change there instead."
)


class ForkSessionTool(HandlerBase):
    """把当前会话分叉出去执行一份子会话；投递任务说明后当场完成，结论由编排汇报。"""

    name: str = TOOL_FORK_SESSION
    description: str = (
        "Fork this conversation into a new sidebar conversation and hand it one part of the "
        "work. It inherits your identity, tools and the conversation up to your previous reply. "
        "The current turn, including the input that started it and all work done in it, is "
        "excluded. Supply a complete new assignment with goal, constraints, necessary new facts and expected deliverable. Use it to split a task into parts that each need this context; "
        "use delegate_task instead when a specialised child agent should do a self-contained "
        "session. The fork does not see anything said here after it starts, and the user can open "
        "and talk to it. The call returns once the fork is created; the fork reports to you "
        "automatically with an <agent_message report=...> each time it finishes a run of your "
        "assignment, so wait for it with wait_messages, or keep doing work that does not overlap."
    )
    args_model: type[ForkSessionArgs] = ForkSessionArgs
    timeout_seconds: ClassVar[float] = 300.0
    group = TOOL_GROUP_CHILD_AGENT

    def __init__(self) -> None:
        self._session_service = get_session_service()
        self._ledger = get_run_ledger()

    def execute(
        self,
        title: str,
        message: str,
        execution_context: ToolExecutionContext,
        writable_root: str | None = None,
    ) -> ToolObservation:
        """复制当前会话、投递交接通知与任务说明，返回已启动的观察。

        失败路径都收口为工具错误观察：源 Session 正被删除等结构性操作占用时提示重试或自己做；
        其余异常记录 ``fork_session_exception`` 后返回通用错误，不向上抛。
        """
        if cancellation_registry.is_cancelled(execution_context.session_id):
            return tool_cancelled(tool_name=self.name)
        try:
            parent_session = self._session_service.get_session(execution_context.session_id)
            # 分叉不换身份：模型与推理强度都沿用发起它的这个 Run（它正在运行，
            # 模型配置必然还在，``from_run`` 不会返回 None）。
            run = self._ledger.get_run(execution_context.run_id)
            launch = cast(RunLaunch, RunLaunch.from_run(run))
            narrowed_root = (
                narrow(execution_context.workspace_root, parent_session.extra.writable_root, writable_root)
                if writable_root
                else None
            )
            fork = self._session_service.fork_for_agent(
                parent_session.id,
                run_id=execution_context.run_id,
                tool_call_id=execution_context.tool_call_id,
                title=title,
                derivation_depth=child_depth(parent_session, "fork"),
                narrowed_root=narrowed_root,
            )

            assignment = prepare_agent_message(
                message,
                source=MessageSource(session_id=parent_session.id, name=parent_session.title),
                agent_id=launch.agent_id,
                model_config_id=launch.model_config_id,
                reasoning_effort=launch.reasoning_effort,
            )
            # 通知先入队：不唤醒、不开 Run，随任务说明开出的第一个 Run 写在它前面。
            handover = _HANDOVER_NOTICE.format(fork_id=fork.id, source_id=parent_session.id)
            fork_root = fork.extra.writable_root
            if fork_root is not None:
                handover += _WRITABLE_ROOT_NOTICE.format(writable_root=fork_root)
            session_runtime_spaces.enqueue(
                fork.id, SessionMessage.of(NoticeBody(content=handover, notice_kind="fork_handover"))
            )
            session_runtime_spaces.enqueue(fork.id, assignment)
            return tool_success(
                tool_name=self.name,
                content=json.dumps(
                    {"status": "started", "fork_session_id": fork.id, "title": title},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                display_data=self._fork_display_data(
                    title=title, child_session_id=fork.id, writable_root=fork_root
                ),
            )
        except WritableRootError as exc:
            return tool_error(self.name, f"could not fork this conversation: {exc}")
        except SessionForkConflictError as exc:
            log.warning(
                "fork_session_conflict",
                extra={
                    "msg": "分叉被拒绝",
                    "data": {"session_id": execution_context.session_id, "code": exc.code},
                },
            )
            return tool_error(
                self.name,
                f"could not fork this conversation: {exc.message}. "
                + ("Retry in a moment." if exc.retryable else "Do the work yourself instead."),
            )
        except Exception as exc:
            log.exception(
                "fork_session_exception",
                extra={
                    "msg": "分叉会话时发生异常",
                    "data": {
                        "session_id": execution_context.session_id,
                        "run_id": execution_context.run_id,
                        "error": str(exc),
                    },
                },
            )
            return tool_error(self.name, "An error occurred while forking the conversation.")

    @staticmethod
    def _fork_display_data(
        *, title: str, child_session_id: int, writable_root: str | None = None
    ) -> dict[str, Any]:
        """分叉卡片：只带分叉会话的定位与标题。

        分叉的运行状态不在这里：展示数据不是事实源，前端按 ``child_session_id`` 读应用级会话状态推送；
        分叉是侧栏会话，前端「打开」跳到它的路由，而不是像委派那样在工作台打开。
        """

        data: dict[str, Any] = {
            "kind": "fork-session-result",
            "title": title,
            "child_session_id": child_session_id,
        }
        if writable_root is not None:
            data["writable_root"] = writable_root
        return data

    def to_definition(self) -> ToolDefinition:
        """工具级 ``parallel``：同一回复里的多个分叉并发建出（复制由 workspace 闸门串行化）。"""
        return ToolDefinition(
            conversation_presentation="standalone",
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            execution_mode="thread",
            parallel_mode="parallel",
        )


def build_fork_session_definition() -> ToolDefinition | None:
    """供工具注册表登记的 fork_session 定义。"""
    return ForkSessionTool().to_definition_if_avaliable()
