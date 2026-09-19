"""工具执行上下文值对象。"""

from dataclasses import dataclass, field
from pathlib import Path

from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.models.workspace_record import WorkspaceRecord


@dataclass(frozen=True)
class ToolExecutionContext:
    """工具执行所在的运行时边界。

    ``ToolExecutionContext`` 表达一次工具调用所处的任务、工作区、根路径与
    turn 边界。``runtime_dependencies`` 承载父进程执行层和同进程工具使用的运行期
    能力，例如委派执行器、输出事件通道工厂和事件 loop。这些依赖可能间接持有
    数据库引擎、事件循环、服务对象或其他不可 pickle 状态。

    process 隔离工具（例如 ``execute_terminal``）启动子进程前必须调用
    :meth:`for_process_execution` 取得跨进程安全副本，避免把
    ``runtime_dependencies`` 一起序列化到子进程。process handler 通过显式注入的
    output sink 将原始输出写入父进程队列；父进程 runner 再使用 runtime channel
    将其投递为运行期事件。process handler 若未来确实需要额外运行期能力，应显式
    设计可序列化 DTO，而不是复用父进程依赖对象。

    Attributes:
        task_id: 当前工具调用所属任务标识。
        workspace_id: 当前工作区标识。
        workspace_root: 当前工具调用允许访问的工作区根路径。
        run_id: 当前工具调用所属 turn 标识；缺省为空字符串。
        trace_id: 当前请求的日志链路标识；进程隔离执行时显式传入子进程。
        runtime_dependencies: 同进程工具可用的运行期依赖，跨进程执行时必须清空。
    """

    task_id: int
    workspace_id: int
    workspace_root: Path
    run_id: int = 0
    trace_id: str = ""
    # Runtime-only locator for the currently executing tool. It is intentionally
    # not persisted and is cleared from process-isolated copies.
    tool_call_id: str = ""
    runtime_dependencies: ToolRuntimeDependencies = field(default_factory=ToolRuntimeDependencies)

    def for_process_execution(self) -> "ToolExecutionContext":
        """返回可安全传入隔离子进程的上下文副本。

        参数:
            无。
        返回:
            与当前对象拥有相同任务、工作区、根路径、turn 和 trace 边界，但清空
            ``runtime_dependencies`` 的 ``ToolExecutionContext``。
        异常:
            无。
        副作用:
            无。
        """

        return ToolExecutionContext(
            task_id=self.task_id,
            workspace_id=self.workspace_id,
            workspace_root=self.workspace_root,
            run_id=self.run_id,
            trace_id=self.trace_id,
        )

    @classmethod
    def from_workspace(
        cls,
        task_id: int,
        workspace: WorkspaceRecord,
        run_id: int = 0,
    ) -> "ToolExecutionContext":
        """从工作区记录与任务标识构造执行上下文。

        参数:
            task_id: 当前执行所属的任务标识。
            workspace: 解析出的工作区记录，其 ``root_path`` 即工具边界基准根。
            run_id: 当前执行所属的轮次标识；缺省为空字符串。
        返回:
            绑定了该任务、工作区边界与轮次标识的 ``ToolExecutionContext``。
        异常:
            无。
        副作用:
            无。
        """

        return cls(
            task_id=task_id,
            workspace_id=workspace.id,
            workspace_root=Path(workspace.root_path),
            run_id=run_id,
        )
