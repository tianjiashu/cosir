"""send_message 的发起配置来源：委派子 Agent 按 profile 授权重算，Agent 分叉沿用自己的 Run。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import configuration
from app.core.agents.agent_profile import AgentProfileType
from app.core.tool.handler import send_message
from app.core.tool.schemas import ToolExecutionContext
from app.models.conversation_run_options import ConversationRunOptions


@pytest.mark.asyncio
async def test_send_message_rejects_main_profile_before_creating_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """历史 child Task 若绑定主 Agent，follow-up 入口不得创建新 Run。"""

    class _TaskService:
        def get_task(self, task_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                id=task_id,
                title="子任务",
                derived_by=lambda task_id: task_id == 7,
                task_type="delegate_task",
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
    monkeypatch.setattr(send_message, "get_task_service", lambda: task_service)
    monkeypatch.setattr(send_message, "get_workspace_service", _WorkspaceService)
    monkeypatch.setattr(send_message, "get_conversation_run_ledger", lambda: run_service)
    # 非 CHILD profile 在开 Run 之前就被拒绝，调度入口不得被触达。
    monkeypatch.setattr(
        send_message.task_runtime_spaces, "enqueue", lambda *_a, **_k: pytest.fail("enqueued")
    )
    base_registry = configuration.build_agent_registry()
    assert base_registry.resolve("system", "main_agent").agent_type is AgentProfileType.MAIN
    monkeypatch.setattr(configuration, "get_agent_registry", lambda: base_registry)

    tool = send_message.SendMessageTool()
    context = ToolExecutionContext(
        step_id="step-1",
        task_id=7,
        workspace_id=3,
        workspace_root=tmp_path,
        run_id=12,
        event_loop=SimpleNamespace(is_closed=lambda: False),  # type: ignore[arg-type]
    )

    result = tool.execute(11, "Continue this task.", execution_context=context)

    assert result.status == "error"
    assert "no longer available" in result.content


def test_send_message_to_a_fork_reuses_its_own_launch_without_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """分叉不换身份：新 Run 沿用它当前 Run 的发起配置，不按子 Agent profile 解析。"""

    fork_task = SimpleNamespace(
        id=41,
        title="前端部分",
        derived_by=lambda task_id: task_id == 7,
        task_type="fork",
        workspace_id=3,
        current_run_id=11,
    )
    parent_task = SimpleNamespace(id=7, title="主对话")
    monkeypatch.setattr(
        send_message,
        "get_task_service",
        lambda: SimpleNamespace(
            get_task=lambda task_id: fork_task if task_id == 41 else parent_task
        ),
    )
    monkeypatch.setattr(send_message, "get_workspace_service", lambda: None)
    monkeypatch.setattr(
        send_message,
        "get_conversation_run_ledger",
        lambda: SimpleNamespace(
            get_run=lambda _run_id: SimpleNamespace(
                id=11,
                status="completed",
                agent_id="main_agent",
                model_config_id=3,
                options=ConversationRunOptions(
                    reasoning_effort="high", disabled_tool_groups=["浏览器工具"]
                ),
            )
        ),
    )
    monkeypatch.setattr(
        configuration, "get_agent_registry", lambda: pytest.fail("resolved a child profile")
    )
    prepared: list[dict] = []
    monkeypatch.setattr(
        send_message,
        "prepare_agent_message",
        lambda text, **kwargs: prepared.append(kwargs) or "agent-message",
    )
    enqueued: list[tuple[int, str]] = []
    monkeypatch.setattr(
        send_message.task_runtime_spaces,
        "enqueue",
        lambda task_id, message: enqueued.append((task_id, message)),
    )

    def run_child(_context, child_task_id, *, label, enqueue, placeholder):
        enqueue()
        return placeholder()

    monkeypatch.setattr(send_message, "run_child", run_child)
    context = ToolExecutionContext(
        step_id="step-1",
        task_id=7,
        workspace_id=3,
        workspace_root=tmp_path,
        run_id=12,
        event_loop=SimpleNamespace(is_closed=lambda: False),  # type: ignore[arg-type]
    )

    result = send_message.SendMessageTool().execute(
        41, "再补个测试", execution_context=context
    )

    assert result.status == "success"
    [kwargs] = prepared
    assert (kwargs["agent_id"], kwargs["model_config_id"]) == ("main_agent", 3)
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["disabled_tool_groups"] == ["浏览器工具"]
    assert enqueued == [(41, "agent-message")]
