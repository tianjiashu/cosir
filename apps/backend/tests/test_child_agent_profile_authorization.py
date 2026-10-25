"""子 Agent 创建入口的 profile 类型授权测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import configuration
from app.core.agents.agent_profile import AgentProfileType
from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.child_agent import child_agent_send


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
        def get_run(self, _run_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                id=11, status="completed", agent_id="main_agent", model_config_id=1
            )

    task_service = _TaskService()
    run_service = _RunService()
    monkeypatch.setattr(child_agent_send, "get_task_service", lambda: task_service)
    monkeypatch.setattr(child_agent_send, "get_workspace_service", _WorkspaceService)
    monkeypatch.setattr(child_agent_send, "get_conversation_run_service", lambda: run_service)
    # 非 CHILD profile 在开 Run 之前就被拒绝，调度入口不得被触达。
    monkeypatch.setattr(child_agent_send, "get_task_actors", lambda: pytest.fail("opened run"))
    base_registry = configuration.build_agent_registry()
    assert base_registry.resolve("system", "main_agent").agent_type is AgentProfileType.MAIN
    monkeypatch.setattr(configuration, "get_agent_registry", lambda: base_registry)

    tool = child_agent_send.ChildAgentSendTool()
    context = ToolExecutionContext(
        step_id="step-1",
        task_id=7,
        workspace_id=3,
        workspace_root=tmp_path,
        run_id=12,
    )

    result = tool.execute(11, "Continue this task.", execution_context=context)

    assert result.status == "error"
