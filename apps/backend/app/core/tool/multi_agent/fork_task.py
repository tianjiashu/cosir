"""fork_task 工具：把当前对话复制成一条侧栏可见的分叉对话，交给它一份子任务，当场完成。

与 ``delegate_task`` 的分工：委派换一个子 Agent 身份、只带任务说明；fork 不换身份，带着到上一条回复为止的
上下文去做。两者共用派生的其余部分：派生深度（``core.tool.derivation.child_depth``）、以 Agent 消息
投递任务说明、Run 落定时由编排向本 Task 汇报（``derived_report``），父 Agent 用 ``wait_messages``
等汇报。用户在分叉里直接对话开出的 Run 不汇报。

继承的历史是第一人称的，分叉自己分不清哪些事归它：曾把来源没做完的请求接过去，并自认是父会话。
所以任务说明前先投一条交接通知，写清身份与任务归属；通知不进请求前缀，不影响缓存。

分叉是侧栏对话（``task_type="agent_fork"``，来源记在 ``extra.fork``），不挂在
所有权子树上：删除父对话时按用户 fork 的规则改挂，而不是一起删掉。父 Task 被上游收回时，分叉按
派生关系一并收回（``TaskRecall``）；用户停止父 Task 不停分叉。
"""

import json
from typing import Any, ClassVar, cast

from app.config.logging.logger import log
from app.core.tool.multi_agent.agent_message import prepare_agent_message
from app.core.tool.multi_agent.args.fork_task_args import ForkTaskArgs
from app.core.tool.multi_agent.derivation import child_depth
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_FORK_TASK,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.message_queue.task_message import NoticeBody, RunLaunch, TaskMessage
from app.models.conversation_input import MessageSource
from app.models.errors.task_fork_errors import TaskForkConflictError
from app.service.depends import get_conversation_run_ledger, get_task_service
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

_HANDOVER_NOTICE = (
    "You are task {fork_id}, a fork of task {source_id}. The conversation above was copied from "
    "that task as background: requests in it belong to that task, even where they read as your "
    "own work, so do not pick them up. Your only job is the assignment in the next message. Each "
    "time you finish working on it, your final reply is reported to task {source_id} "
    "automatically, so do not send it with send_message. If the user later talks to you here, "
    "follow the user; what you do for the user is not reported."
)


class ForkTaskTool(HandlerBase):
    """把当前对话分叉出去执行一份子任务；投递任务说明后当场完成，结论由编排汇报。"""

    name: str = TOOL_FORK_TASK
    description: str = (
        "Fork this conversation into a new sidebar conversation and hand it one part of the "
        "work. It inherits your identity, tools and the conversation up to your previous reply. "
        "The current turn, including the input that started it and all work done in it, is "
        "excluded. Supply a complete new assignment with goal, constraints, necessary new facts and expected deliverable. Use it to split a task into parts that each need this context; "
        "use delegate_task instead when a specialised child agent should do a self-contained "
        "task. The fork does not see anything said here after it starts, and the user can open "
        "and talk to it. The call returns once the fork is created; the fork reports to you "
        "automatically with an <agent_message report=...> each time it finishes a run of your "
        "assignment, so wait for it with wait_messages, or keep doing work that does not overlap."
    )
    args_model: type[ForkTaskArgs] = ForkTaskArgs
    timeout_seconds: ClassVar[float] = 300.0
    group = TOOL_GROUP_CHILD_AGENT

    def __init__(self) -> None:
        self._task_service = get_task_service()
        self._ledger = get_conversation_run_ledger()

    def execute(
        self,
        title: str,
        message: str,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """复制当前对话、投递交接通知与任务说明，返回已启动的观察。

        失败路径都收口为工具错误观察：源 Task 正被删除等结构性操作占用时提示重试或自己做；
        其余异常记录 ``fork_task_exception`` 后返回通用错误，不向上抛。
        """
        if cancellation_registry.is_cancelled(execution_context.task_id):
            return tool_cancelled(tool_name=self.name)
        try:
            parent_task = self._task_service.get_task(execution_context.task_id)
            # 分叉不换身份：模型、推理强度与关掉的工具组都沿用发起它的这个 Run（它正在运行，
            # 模型配置必然还在，``from_run`` 不会返回 None）。
            run = self._ledger.get_run(execution_context.run_id)
            launch = cast(RunLaunch, RunLaunch.from_run(run))
            fork = self._task_service.fork_for_agent(
                parent_task.id,
                run_id=execution_context.run_id,
                tool_call_id=execution_context.tool_call_id,
                title=title,
                derivation_depth=child_depth(parent_task, "fork"),
            )

            assignment = prepare_agent_message(
                message,
                source=MessageSource(task_id=parent_task.id, name=parent_task.title),
                agent_id=launch.agent_id,
                model_config_id=launch.model_config_id,
                reasoning_effort=launch.options.reasoning_effort,
                disabled_tool_groups=launch.options.disabled_tool_groups,
            )
            # 通知先入队：不唤醒、不开 Run，随任务说明开出的第一个 Run 写在它前面。
            handover = _HANDOVER_NOTICE.format(fork_id=fork.id, source_id=parent_task.id)
            task_runtime_spaces.enqueue(
                fork.id, TaskMessage.of(NoticeBody(content=handover, notice_kind="fork_handover"))
            )
            task_runtime_spaces.enqueue(fork.id, assignment)
            return tool_success(
                tool_name=self.name,
                content=json.dumps(
                    {"status": "started", "fork_task_id": fork.id, "title": title},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                display_data=self._fork_display_data(title=title, child_task_id=fork.id),
            )
        except TaskForkConflictError as exc:
            log.warning(
                "fork_task_conflict",
                extra={
                    "msg": "分叉被拒绝",
                    "data": {"task_id": execution_context.task_id, "code": exc.code},
                },
            )
            return tool_error(
                self.name,
                f"could not fork this conversation: {exc.message}. "
                + ("Retry in a moment." if exc.retryable else "Do the work yourself instead."),
            )
        except Exception as exc:
            log.exception(
                "fork_task_exception",
                extra={
                    "msg": "分叉对话时发生异常",
                    "data": {
                        "task_id": execution_context.task_id,
                        "run_id": execution_context.run_id,
                        "error": str(exc),
                    },
                },
            )
            return tool_error(self.name, "An error occurred while forking the conversation.")

    @staticmethod
    def _fork_display_data(*, title: str, child_task_id: int) -> dict[str, Any]:
        """分叉卡片：只带分叉对话的定位与标题。

        分叉的运行状态不在这里：展示数据不是事实源，前端按 ``child_task_id`` 读应用级任务状态推送；
        分叉是侧栏对话，前端「打开」跳到它的路由，而不是像委派那样在工作台打开。
        """

        return {"kind": "fork-task-result", "title": title, "child_task_id": child_task_id}

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


def build_fork_task_definition() -> ToolDefinition | None:
    """供工具注册表登记的 fork_task 定义。"""
    return ForkTaskTool().to_definition_if_avaliable()
