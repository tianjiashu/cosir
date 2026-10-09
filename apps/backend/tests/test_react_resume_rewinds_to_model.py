"""续跑（resume）一律回退到 model 节点重跑的验证。

独立测试方：只验证 ``ReactLikeWorkflow.run`` 在 ``execution_mode="resume"`` 时
为 ``graph.astream`` 选择的输入，不连接真实模型 / 数据库 / checkpointer。

背景：工具执行期崩溃 → 后端重启收敛为 cancelled → 用户续跑时，若从 ``tools_node`` /
``observe_node`` 重入会把整批工具调用静默重放（高危）。修复后，除「等待用户决定」的
``wait_user`` 中断态需要把用户决定作为 resume 载荷喂入该节点外，其余中断（用户取消 /
工具阶段崩溃 / 重启收敛）一律 ``Command(goto="model")`` 回退到 model 节点重跑，由模型重新决策。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langgraph.types import Command

from app.core.workflows.agent_workflow import WorkflowRunFailure
from app.core.workflows.react import workflow as workflow_module
from app.core.workflows.react.workflow import ReactLikeWorkflow


def _goto_target(cmd: Command | None) -> object | None:
    """取 ``Command(goto=...)`` 展开后的首个目标节点名。"""

    if cmd is None:
        return None
    goto = getattr(cmd, "goto", None)
    if isinstance(goto, (tuple, list)):
        return goto[0] if goto else None
    return goto


def _resume_target(cmd: Command | None) -> object | None:
    """取 ``Command(resume=...)`` 的 resume 负载。"""

    return None if cmd is None else getattr(cmd, "resume", None)


class _FakeGraph:
    """捕获传入 ``astream`` 的输入，并满足 ``run`` 收尾所需的只读接口。"""

    def __init__(self, *, next_nodes: tuple[str, ...], tasks: list[object] | None = None) -> None:
        self._next = tuple(next_nodes)
        self._tasks = list(tasks or [])
        self.captured_input: object | None = None

    async def aget_state(self, _config: object) -> SimpleNamespace:
        return SimpleNamespace(next=self._next, tasks=self._tasks, values={"terminal_sessions": {}})

    async def aupdate_state(self, _config: object, _values: object) -> None:
        return None

    async def astream(self, input_state: object, _config: object, *, stream_mode: object = None) -> object:
        self.captured_input = input_state
        if False:  # 让函数成为 async generator，但不产出任何 chunk
            yield


def _make_operations(*, extra: object = None) -> SimpleNamespace:
    run = SimpleNamespace(
        id=1,
        checkpoint_thread_id="thread-1",
        input_text="hi",
        image_paths=[],
        extra=extra,
    )
    agent_profile = SimpleNamespace(
        model_settings=SimpleNamespace(model_name="x"),
        structured_output=None,
        max_steps=10,
    )
    return SimpleNamespace(
        get_current_run=lambda: run,
        get_current_task=lambda: SimpleNamespace(id=1),
        agent_profile=agent_profile,
        task_tool_schemas=[],
        wait_run_for_input_if_running=lambda: SimpleNamespace(id=1),
    )


def _make_task_space() -> SimpleNamespace:
    manager = SimpleNamespace(
        begin_run=lambda *a, **k: None,
        ensure_run_user_message=lambda *a, **k: None,
    )
    deferred: list[object] = []

    # 进程内 conversation_run 级运行时上下文（``task_runtime_spaces`` 经 ``get_or_create`` 返回的对象）：
    # 提供 ``get_context_manager``（manager 桩）与 ``defer_system_message``（记录注入的提示）。
    space = SimpleNamespace(
        get_context_manager=lambda **k: manager,
        defer_system_message=lambda message: deferred.append(message),
    )
    # 注册表 ``task_runtime_spaces`` 本身只暴露 ``get_or_create``，恒返回同一个 conversation_run space。
    registry = SimpleNamespace(get_or_create=lambda _task_id: space)
    registry._deferred = deferred
    return registry


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """注入 ``run`` 的全部外部依赖，返回可观测的假对象。"""

    space = _make_task_space()

    @asynccontextmanager
    async def _checkpointer() -> object:
        yield object()

    monkeypatch.setattr(workflow_module, "build_checkpointer", _checkpointer)
    monkeypatch.setattr(
        ReactLikeWorkflow, "_build_graph", staticmethod(lambda _cp: _FakeGraph(next_nodes=()))
    )
    monkeypatch.setattr(
        workflow_module,
        "resolve_chat_model",
        lambda **k: SimpleNamespace(model=object(), supports_thinking=False),
    )
    monkeypatch.setattr(
        workflow_module,
        "get_terminal_session_service",
        lambda: SimpleNamespace(close_run_terminals=lambda *a, **k: None),
    )
    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space_registry.task_runtime_spaces",
        space,
    )
    return space


async def _run_with(
    patched: SimpleNamespace,
    *,
    next_nodes: tuple[str, ...],
    tasks: list[object] | None = None,
    expect_failure: bool = False,
    execution_mode: str = "resume",
    user_decisions: tuple[object, ...] = (),
) -> _FakeGraph:
    """装配图（停在 ``next_nodes``）并驱动一次续跑，返回捕获了输入的假图。

    ``execution_mode`` 决定输入构造分支：``resume``（无用户决定）一律回退 model，
    ``resume_with_input``（携带用户决定）才会注入 ``Command(resume=...)``。
    ``expect_failure`` 为真时要求 ``run`` 抛出 ``WorkflowRunFailure``（续跑准入被拒路径）；
    此时异常不再由工作流吞掉，而是交给运行时落定 failed 终态。
    """

    import unittest.mock as mock

    graph = _FakeGraph(next_nodes=next_nodes, tasks=tasks)
    with mock.patch.object(ReactLikeWorkflow, "_build_graph", staticmethod(lambda _cp: graph)):
        drive = ReactLikeWorkflow().run(
            operations=_make_operations(),
            execution_mode=execution_mode,  # type: ignore[arg-type]
            user_decisions=user_decisions,  # type: ignore[arg-type]
            callbacks=[],
        )
        if expect_failure:
            with pytest.raises(WorkflowRunFailure):
                await drive
        else:
            await drive
    return graph


async def test_resume_crash_in_tools_rewinds_to_model(patched: SimpleNamespace) -> None:
    """崩在 tools 节点的续跑：``input_state`` 必须是 ``Command(goto="model")``。

    重跑提示由 ``model_node`` 在进入模型请求前自行注入（见 ``_build_operations`` 投递的工具
    准入消息与 ``model_node`` 的续写提示），``run`` 自身不投递任何延迟消息。
    """

    graph = await _run_with(patched, next_nodes=("tools",))
    assert _goto_target(graph.captured_input) == "model"
    assert patched._deferred == []


async def test_resume_crash_in_observe_rewinds_to_model(patched: SimpleNamespace) -> None:
    """崩在 observe 节点的续跑：同样回退到 model。"""

    graph = await _run_with(patched, next_nodes=("observe",))
    assert _goto_target(graph.captured_input) == "model"


async def test_resume_user_cancel_at_model_rewinds_to_model(patched: SimpleNamespace) -> None:
    """用户取消（interrupt 在 model）的续跑：从 model 节点重跑，而非续写半截输出。"""

    graph = await _run_with(patched, next_nodes=("model",))
    assert _goto_target(graph.captured_input) == "model"


async def test_resume_with_input_waits_user_keeps_command_resume(
    patched: SimpleNamespace,
) -> None:
    """等待用户决定的续跑：``resume_with_input`` 下保留 ``Command(resume=...)``，不回退 model。"""

    graph = await _run_with(
        patched,
        next_nodes=("wait_user",),
        tasks=[SimpleNamespace(name="wait_user", interrupts=[object()])],
        execution_mode="resume_with_input",
    )
    assert _goto_target(graph.captured_input) is None
    # 无决定时注入空决定集合：``wait_user`` 据此重新挂起同一请求，而不是把空值当批准。
    assert _resume_target(graph.captured_input) == {"decisions": []}
    assert patched._deferred == []


async def test_resume_with_input_without_interrupt_passes_no_input(
    patched: SimpleNamespace,
) -> None:
    """``resume_with_input`` 但恢复现场没有 interrupt（决定迟到）时不注入任何输入。

    该分支既不注入 ``resume`` 载荷、也不强制 ``goto="model"``：``astream(None)`` 由 LangGraph
    从既有 checkpoint 继续，避免把决定喂给一个已经不在等待的图。
    """

    graph = await _run_with(
        patched,
        next_nodes=("model",),
        tasks=[SimpleNamespace(name="model", interrupts=[])],
        execution_mode="resume_with_input",
    )
    assert graph.captured_input is None


async def test_resume_finished_graph_is_rejected(patched: SimpleNamespace) -> None:
    """图已走到 END（无 next）：拒绝续跑，抛带失败码的异常，不向 astream 注入任何输入。"""

    graph = await _run_with(patched, next_nodes=(), expect_failure=True)
    assert graph.captured_input is None
