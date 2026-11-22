"""fork_session 工具：把当前会话复制成一条侧栏可见的分叉会话，交给它一份任务说明，当场完成。

与 ``delegate_task`` 的分工：委派换一个子 Agent 身份、只带任务说明；fork 不换身份，带着到上一条回复为止的
上下文去做。两者共用派生的其余部分：派生深度（``core.tool.multi_agent.derivation.child_depth``）、以 Agent 消息
投递任务说明、Run 落定时由编排向本 Session 汇报（``derived_report``），父 Agent 用 ``wait_messages``
等汇报。用户在分叉里直接对话开出的 Run 不汇报。

任务说明前的交接通知（身份、会话归属、可写根）是分叉自己的事实，由分叉层随分支在同一事务里写入
（``ForkSource.identity_notice``），这里只投递任务说明。

分叉是侧栏会话（``session_type="agent_fork"``，来源记在 ``extra.fork``），不挂在
所有权子树上：删除父对话时按用户 fork 的规则改挂，而不是一起删掉。父 Session 被上游收回时，分叉按
派生关系一并收回（``SessionRecall``）；用户停止父 Session 不停分叉。
"""

import json
from typing import Any, cast

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
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.tool.tool_base import HandlerBase
from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry
from app.core.workflow.message_queue.session_message import RunLaunch
from app.models.message_context.conversation_input import MessageSource
from app.models.session.session_fork_errors import SessionForkConflictError
from app.service.depends import get_run_ledger, get_session_service
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces


class ForkSessionTool(HandlerBase):
    """把当前会话分叉出去做一部分工作；投递任务说明后当场完成，结论由编排汇报。"""

    name: str = TOOL_FORK_SESSION
    # 「何时用」对照 Claude Code 的 Agent 工具与派生会话规则改写，只取适用于分叉的部分：分叉带着上下文，
    # 「它只知道你写的」不成立，反过来要强调本回合不可见、它与你同一套推理不能当独立核查。与委派共用的
    # 部分（代价、自己做还是交出去、汇报怎么来与怎么对待）在系统提示词工具层写一次
    # （``SystemPromptBuilder._handoff_rules``），这里只留分叉独有的。
    description: str = (
        "Fork this conversation into a new sidebar conversation and hand it one part of the "
        "work. It inherits your identity, model, tools and the conversation up to your previous "
        "reply. The current turn — the input that started it and all work done in it — is "
        "excluded, so restate in your own words whatever from this turn the fork needs: a line "
        "like 'based on the findings above' points at nothing.\n"
        "\n"
        "When to use:\n"
        "Use it to split a task into parts that each need this conversation's context; use "
        "delegate_task instead when a specialised child agent should do a self-contained piece, "
        "or when the part needs little of a long conversation. A fork carries this whole "
        "conversation into every request, and it shares your reasoning so far, so it is no "
        "independent check: for a review not anchored on yours, use delegate_task and give it the "
        "material, not your conclusion.\n"
        "\n"
        "After the call:\n"
        "- It returns once the fork is created. The fork does not see anything said here after it "
        "starts, and the user can open it and talk to it."
    )
    args_model: type[ForkSessionArgs] = ForkSessionArgs
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
        """复制当前会话（交接通知随分支写入）、投递任务说明，返回已启动的观察。

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
                narrow(
                    execution_context.workspace_root,
                    parent_session.extra.writable_root,
                    writable_root,
                )
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
                source=MessageSource(
                    session_id=parent_session.id, name=parent_session.title, from_deriver=True
                ),
                launch=launch,
            )
            # 交接通知已随分支写入队列（优先级在前），任务说明开出的第一个 Run 先读到身份再读到任务。
            fork_root = fork.extra.writable_root
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
            # 分叉是少数协作场景才用，延迟加载。
            deferred=True,
        )


def build_fork_session_definition() -> ToolDefinition:
    """供工具注册表登记的 fork_session 定义。"""
    return ForkSessionTool().to_definition()
