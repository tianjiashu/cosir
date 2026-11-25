"""``terminal_write`` handler：把一行命令填进用户终端的空提示符，不执行、不等待用户回车。

没指定终端时先复用空闲的，没有就在工作区根新开一个再填：用户不必先自己开终端。新开的终端与用户
自己开的一样（不进沙箱、由用户关闭），执行仍然只能由用户回车触发，所以「终端里的每一次执行都由人触发」
这条不变式不变。
"""

from dataclasses import replace

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tool.schemas import (
    TOOL_TERMINAL_WRITE,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.core.tool.tool_base import HandlerBase
from app.core.workflow.cancel.tool_call_cancellation_registry import should_stop_call
from app.service import depends as service_depends
from app.tools.terminal_runtime.errors import TerminalPrefillRejectedError
from app.tools.terminal_runtime.session.session_runtime import TerminalSessionRuntime
from app.tools.terminal_runtime.session.shell_resolver import prefill_shell
from app.tools.tool_handler.terminal.tool_observation import (
    observation_result,
    with_terminal_errors,
)
from app.tools.tool_models import TerminalWriteArgs


class TerminalWriteTool(HandlerBase):
    name = TOOL_TERMINAL_WRITE
    # 中文译文：把一行命令填进用户的终端提示符，但不执行：用户看过之后自己按回车，你也不会知道他们按没按
    # （之后用 terminal_read 看发生了什么）。终端面板会展开并显示这一行。用户让你把命令放进终端、或某一步
    # 需要用户亲自在终端里运行时用它。不指定 terminal_id 时，用一个停在空提示符的终端；都在忙或还没有终端，
    # 就在工作区根新开一个。指定 terminal_id 时，只有那个终端 shell 集成就绪、停在提示符且还没有任何输入
    # 才可用。同一时刻只能有一条用户还没碰过的填入：用户还没碰上一条（按任意键、哪怕只移动光标都算接手）
    # 时会失败，改在回复里给出命令即可。
    # 只能一行：多步操作先写脚本文件，再填运行它的那一行命令。
    description = (
        "Fill one command line into a prompt in the user's terminal, without running it: the user "
        "reviews it and presses Enter themselves, and you are not told whether they did (read the "
        "terminal later to see what happened). The terminal panel opens to show it. Use it when the "
        "user asks you to put a command in their terminal, or when a step needs the user to run it "
        "themselves. Without terminal_id it uses a terminal idle at an empty prompt, or opens a new "
        "one at the workspace root if every terminal is busy or there is none. With terminal_id it "
        "works only while that shell is idle at its prompt with nothing typed yet (shell "
        "integration required). Only one filled command can sit untouched at a time: while the "
        "user has not touched the previous one (any key, even moving the cursor, takes it over), it "
        "fails and you can give the command in your reply. One "
        "line only: for several steps, write a script file first and fill in the command that runs it."
    )
    args_model = TerminalWriteArgs
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        terminal_id: str | None,
        command: str,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """命令由参数边界校验；填入立即返回，用户回车与之后输出由 terminal_read 观察。"""

        def action() -> ToolObservation:
            self._refuse_second_pending(execution_context.session_id)
            if terminal_id is not None:
                # 填入只针对用户终端：不经 ``find_terminal``（运行中的命令不能输入）。
                runtime = service_depends.get_terminal_session_service().require(
                    terminal_id, execution_context.session_id
                )
                opened = False
            else:
                runtime, opened = self._idle_or_new(execution_context)
            result = runtime.prefill(
                command,
                run_id=execution_context.run_id,
                tool_call_id=execution_context.tool_call_id,
            )
            if opened:
                result = replace(
                    result,
                    text=(
                        f"opened a new terminal {runtime.terminal_session_id} at the workspace root "
                        f"because no terminal was idle at an empty prompt\n{result.text}"
                    ),
                )
            return observation_result(self.name, result)

        return with_terminal_errors(self.name, action)

    @staticmethod
    def _refuse_second_pending(session_id: int) -> None:
        """同一 Session 同时只留一条用户还没碰过的填入（``prefill_pending``；用户按任意键即接手）。

        否则用户还没处理上一条时 Agent 再填，空闲终端已被占住，就会再新开一个，终端越堆越多；一件
        等用户的事也比几件散在不同终端里清楚。
        """
        for runtime in service_depends.get_terminal_session_service().session_runtimes(session_id):
            if runtime.prefill_pending:
                raise TerminalPrefillRejectedError(
                    f"a command you filled into terminal {runtime.terminal_session_id} has not "
                    "been touched by the user yet"
                )

    @classmethod
    def _idle_or_new(
        cls, execution_context: ToolExecutionContext
    ) -> tuple[TerminalSessionRuntime, bool]:
        """最近新建的空闲终端；没有就新开一个并等到第一个提示符。返回终端与「是否新开」。"""
        service = service_depends.get_terminal_session_service()
        session_id = execution_context.session_id
        idle = [runtime for runtime in service.session_runtimes(session_id) if runtime.accepts_prefill]
        if idle:
            return max(idle, key=lambda runtime: runtime.created_at), False
        runtime = service.start(
            session_id=session_id, cwd=str(execution_context.workspace_root), shell=prefill_shell()
        )
        log.info(
            "terminal_agent_session_opened",
            extra={
                "msg": "没有空闲终端，Agent 为填入新开用户终端",
                "data": {
                    "session_id": session_id,
                    "run_id": execution_context.run_id,
                    "tool_call_id": execution_context.tool_call_id,
                    "terminal_session_id": runtime.terminal_session_id,
                    "shell_kind": runtime.spec.kind,
                },
            },
        )
        outcome = runtime.wait_for_prompt(
            Constant.Terminal.PREFILL_PROMPT_TIMEOUT_SECONDS,
            lambda: should_stop_call(session_id, execution_context.tool_call_id),
        )
        if outcome == "timeout":
            # 终端留给用户：启动脚本慢不等于终端不能用，关掉反而丢了用户可能正要用的 shell。
            raise TerminalPrefillRejectedError(
                f"opened terminal {runtime.terminal_session_id}, but its shell did not reach a "
                "prompt in time"
            )
        if outcome == "cancelled":
            raise TerminalPrefillRejectedError(
                f"cancelled while terminal {runtime.terminal_session_id} was starting; nothing was filled"
            )
        return runtime, True

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            needs_user_surface=True,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
        )
