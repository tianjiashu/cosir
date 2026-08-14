"""验证 process 工具执行时不会携带不可 pickle 的运行期依赖。"""

import pickle
from pathlib import Path

from pydantic import BaseModel

from app.tools.schemas import ToolDefinition, ToolExecutionContext
from app.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.tools.tool_execute.tool_executor import ToolExecutor
from app.tools.tool_handler.execute_terminal import build_execute_terminal_definition


class _EmptyArgs(BaseModel):
    """测试用空参数模型。"""


class _UnpickleableDependency:
    """模拟持有 SQLAlchemy engine / event loop 等不可 pickle 状态的运行期依赖。"""

    def __getstate__(self) -> object:
        """模拟 pickle 失败。

        参数:
            无。
        返回:
            无。
        异常:
            TypeError: 始终抛出，模拟不可跨进程序列化对象。
        副作用:
            无。
        """

        raise TypeError("cannot pickle runtime dependency")


def _make_process_tool() -> ToolDefinition:
    """构造 process 模式测试工具定义。

    参数:
        无。
    返回:
        execution_mode 为 ``process`` 的 ``ToolDefinition``。
    异常:
        无。
    副作用:
        无。
    """

    return ToolDefinition(
        name="process_tool",
        description="process tool",
        permission="execute",
        handler=lambda **_kwargs: None,
        args_model=_EmptyArgs,
        execution_mode="process",
    )


def test_tool_execution_context_process_safe_copy_drops_runtime_dependencies() -> None:
    """跨进程安全副本应清空运行期依赖并可被 pickle。

    参数:
        无。
    返回:
        无。
    异常:
        AssertionError: 当副本仍携带不可 pickle 依赖时由 pytest 抛出。
    副作用:
        触发一次 pickle 序列化验证。
    """

    context = ToolExecutionContext(
        task_id="task-1",
        workspace_id="workspace-1",
        workspace_root=Path("."),
        turn_id="turn-1",
        runtime_dependencies=ToolRuntimeDependencies(
            delegate_task_executor=_UnpickleableDependency(),  # type: ignore[arg-type]
        ),
    )

    safe_context = context.for_process_execution()

    assert safe_context is not context
    assert safe_context.task_id == context.task_id
    assert safe_context.workspace_id == context.workspace_id
    assert safe_context.workspace_root == context.workspace_root
    assert safe_context.turn_id == context.turn_id
    assert safe_context.runtime_dependencies == ToolRuntimeDependencies()
    pickle.dumps(safe_context)


def test_process_executor_starts_with_process_safe_context(monkeypatch) -> None:
    """process 模式应只把跨进程安全 context 传给子进程。

    参数:
        monkeypatch: pytest 注入的 monkeypatch fixture。
    返回:
        无。
    异常:
        AssertionError: 当 ``multiprocessing.Process`` 收到原始 context 时由 pytest 抛出。
    副作用:
        替换 ``multiprocessing.Process`` 为记录参数的 fake。
    """

    captured: dict[str, object] = {}

    class _FakeProcess:
        """记录 Process 构造参数并模拟启动失败。"""

        def __init__(self, *, target, args, daemon) -> None:
            """记录构造参数。

            参数:
                target: 子进程入口。
                args: 子进程入口参数。
                daemon: daemon 标记。
            返回:
                无。
            异常:
                无。
            副作用:
                写入 ``captured``。
            """

            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self) -> None:
            """模拟进程启动失败以终止执行路径。

            参数:
                无。
            返回:
                无。
            异常:
                RuntimeError: 始终抛出，避免测试真正启动子进程。
            副作用:
                无。
            """

            raise RuntimeError("stop after construction")

    monkeypatch.setattr(
        "app.tools.tool_execute.tool_executor.multiprocessing.Process",
        _FakeProcess,
    )
    monkeypatch.setattr("app.tools.tool_execute.tool_executor.get_log_queue", lambda: None)

    context = ToolExecutionContext(
        task_id="task-1",
        workspace_id="workspace-1",
        workspace_root=Path("."),
        turn_id="turn-1",
        runtime_dependencies=ToolRuntimeDependencies(
            delegate_task_executor=_UnpickleableDependency(),  # type: ignore[arg-type]
        ),
    )

    result = ToolExecutor().execute(
        _make_process_tool(),
        {},
        execution_context=context,
        tool_call_id="call-1",
    )

    process_args = captured["args"]
    process_context = process_args[4]
    assert process_context is not context
    assert process_context.runtime_dependencies == ToolRuntimeDependencies()
    pickle.dumps(process_context)
    assert result.status == "error"
    assert "stop after construction" in result.error


def test_execute_terminal_process_mode_ignores_unpickleable_runtime_dependencies(
    tmp_path,
) -> None:
    """execute_terminal 带不可 pickle 运行期依赖时仍应能启动子进程执行。

    参数:
        tmp_path: pytest 注入的临时目录 fixture。
    返回:
        无。
    异常:
        AssertionError: 当终端工具仍因 runtime_dependencies pickle 失败时由 pytest 抛出。
    副作用:
        启动一个隔离工具子进程执行安全的 Python 版本查询命令。
    """

    context = ToolExecutionContext(
        task_id="task-1",
        workspace_id="workspace-1",
        workspace_root=tmp_path,
        turn_id="turn-1",
        runtime_dependencies=ToolRuntimeDependencies(
            delegate_task_executor=_UnpickleableDependency(),  # type: ignore[arg-type]
        ),
    )

    result = ToolExecutor().execute(
        build_execute_terminal_definition(),
        {"command": "python --version", "timeout": 10},
        execution_context=context,
        tool_call_id="call-terminal",
    )

    assert result.status == "success"
    assert "Python" in result.content
