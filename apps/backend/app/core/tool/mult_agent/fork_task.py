"""fork_task 工具：把当前对话复制成一条侧栏可见的分叉对话，交给它一份子任务，结论回投给自己。

与 ``delegate_task`` 的分工：委派换一个子 Agent 身份、只带任务说明；fork 不换身份，带着这段对话的
全部上下文去做。两者共用派生的其余部分：派生深度（``core.tool.derivation.child_depth``；到上限的
Task 创建时就拿不到派生工具）、以 Agent 消息投递任务说明、以父调用的 ``tool_call_id`` 登记后台等待
（``child_run_job.run_child``），父 Agent 用 ``wait_messages`` 等结论。

分叉是侧栏对话（``task_type="fork"``，来源记在 ``extra.fork``、``created_by == "agent"``），不挂在
所有权子树上：删除父对话时按用户 fork 的规则改挂，而不是一起删掉。父 Task 停止时，登记表撤销条目，
撤销回调以 ``parent`` 原因停下它还在等的分叉；结论已回投、不再被等的分叉不受影响，用户可以接着聊。
"""

import json
from typing import Any, ClassVar, cast

from app.config.logging.logger import log
from app.core.tool.derivation import child_depth
from app.core.tool.mult_agent.args.fork_task_args import ForkTaskArgs
from app.core.tool.mult_agent.child_agent.child_run_job import run_child
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
from app.core.workflow.message_queue.task_message import RunLaunch
from app.models.conversation_input import MessageSource
from app.models.errors.task_fork_errors import TaskForkConflictError
from app.service.conversation_run.agent_message import prepare_agent_message
from app.service.depends import get_conversation_run_ledger, get_task_service
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class ForkTaskTool(HandlerBase):
    """把当前对话分叉出去执行一份子任务；一律后台，结论稍后以 ``background_result`` 回投。"""

    name: str = TOOL_FORK_TASK
    description: str = (
        "Fork this conversation into a new sidebar conversation and hand it one part of the "
        "work. The fork keeps your identity, tools and this whole conversation up to now, so "
        "the message only needs to say what is new for it: its scope, what to leave to you, and "
        "what to report back. Use it to split a task into parts that each need this context; "
        "use delegate_task instead when a specialised child agent should do a self-contained "
        "task. The fork does not see anything said here after it starts, and the user can open "
        "and talk to it. It always runs in the background: wait for its conclusion with "
        "wait_messages, or keep doing work that does not overlap."
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
        """复制当前对话、投递子任务并登记等待，返回标 ``background`` 的已提交占位。

        失败路径都收口为工具错误观察：源 Task 正被删除等结构性操作占用、父事件循环不可用；
        其余异常记录 ``fork_task_exception`` 后返回通用错误，不向上抛。
        """
        if cancellation_registry.is_cancelled(execution_context.task_id):
            return tool_cancelled(tool_name=self.name)
        loop = execution_context.event_loop
        if loop is None or loop.is_closed():
            return tool_error(
                self.name,
                "The parent runtime event loop is unavailable, so the fork cannot be started; "
                "retry from a normal turn.",
            )
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

            def enqueue() -> None:
                task_runtime_spaces.enqueue(
                    fork.id,
                    prepare_agent_message(
                        message,
                        source=MessageSource(task_id=parent_task.id, name=parent_task.title),
                        agent_id=launch.agent_id,
                        model_config_id=launch.model_config_id,
                        reasoning_effort=launch.options.reasoning_effort,
                        disabled_tool_groups=launch.options.disabled_tool_groups,
                    ),
                )

            def placeholder() -> ToolObservation:
                return tool_success(
                    tool_name=self.name,
                    content=json.dumps(
                        {"status": "submitted", "fork_task_id": fork.id, "title": title},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    display_data=self._fork_display_data(title=title, child_task_id=fork.id),
                )

            return run_child(
                execution_context, fork.id, label=title, enqueue=enqueue, placeholder=placeholder
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
