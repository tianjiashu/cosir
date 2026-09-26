"""子 Agent 创建入口的 profile 类型授权测试。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import configuration
from app.core.agents.agent_profile import AgentProfileType
from app.core.tools.schemas.tool_execution_context import ToolExecutionContext
from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.core.tools.tool_handler.child_task import child_agent_send


@pytest.mark.asyncio
async def test_child_agent_send_rejects_main_profile_before_creating_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """历史 child Task 若绑定主 Agent，follow-up 入口不得创建新 Run。"""

    class _TaskService:
        def get_task(self, _task_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                is_child=True,
                parent_task_id=7,
                workspace_id=3,
                current_run_id=11,
            )

    class _WorkspaceService:
        def get_workspace(self, _workspace_id: int) -> SimpleNamespace:
            return SimpleNamespace(id=3, root_path=str(tmp_path))

    class _RunService:
        created = False

        def create_run(self, **_kwargs) -> None:
            self.created = True
            raise AssertionError("非 CHILD profile 不得创建 follow-up Run")

    class _RunStateService:
        def get_run(self, _run_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                id=11,
                status="completed",
                agent_id="main_agent",
                provider_id=1,
                model_name="model",
                reasoning_effort=None,
            )

    task_service = _TaskService()
    run_service = _RunService()
    monkeypatch.setattr(child_agent_send, "get_task_service", lambda: task_service)
    monkeypatch.setattr(child_agent_send, "get_workspace_service", _WorkspaceService)
    monkeypatch.setattr(child_agent_send, "get_conversation_run_service", lambda: run_service)
    monkeypatch.setattr(
        child_agent_send,
        "get_conversation_run_state_service",
        _RunStateService,
    )
    monkeypatch.setattr(child_agent_send, "get_conversation_run_executor", object)
    base_registry = configuration.build_agent_registry()
    assert base_registry.resolve("system", "main_agent").agent_type is AgentProfileType.MAIN
    monkeypatch.setattr(configuration, "get_agent_registry", lambda: base_registry)

    tool = child_agent_send.ChildAgentSendTool()
    context = ToolExecutionContext(
        task_id=7,
        workspace_id=3,
        workspace_root=tmp_path,
        run_id=12,
        runtime_dependencies=ToolRuntimeDependencies(
            runtime_event_loop=asyncio.get_running_loop(),
        ),
    )

    result = tool.execute(11, "Continue this task.", execution_context=context)

    assert result.status == "error"
    assert run_service.created is False
