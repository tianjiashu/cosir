"""send_message 的发起配置来源：委派子 Agent 按 profile 授权重算，Agent 分叉沿用自己的 Run。"""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import configuration
from app.core.agents.agent_profile import AgentProfileType
from app.core.tool.multi_agent import send_message
from app.core.tool.schemas import ToolExecutionContext


@pytest.fixture(autouse=True)
def target_space(monkeypatch):
    monkeypatch.setattr(send_message.session_runtime_spaces, "get_or_create",
                        lambda _: SimpleNamespace(operation=nullcontext))


@pytest.mark.asyncio
async def test_send_message_rejects_main_profile_before_creating_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """历史 child Session 若绑定主 Agent，follow-up 入口不得创建新 Run。"""

    class _SessionService:
        def get_session(self, session_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                id=session_id,
                title="子会话",
                derived_by=lambda session_id: session_id == 7,
                session_type="delegated",
                parent_session_id=7,
                workspace_id=3,
                current_run_id=11,
            )

    class _WorkspaceService:
        def get_workspace(self, _workspace_id: int) -> SimpleNamespace:
            return SimpleNamespace(id=3, root_path=str(tmp_path))

    class _RunService:
        def require_current_run(self, _session_id: int) -> SimpleNamespace:
            return SimpleNamespace(
                id=11, status="completed", agent_id="main_agent", model_config_id="test/m1"
            )

    session_service = _SessionService()
    run_service = _RunService()
    monkeypatch.setattr(send_message, "get_session_service", lambda: session_service)
    monkeypatch.setattr(send_message, "get_workspace_service", _WorkspaceService)
    session_service.require_current_run = run_service.require_current_run
    # 非 CHILD profile 在开 Run 之前就被拒绝，调度入口不得被触达。
    monkeypatch.setattr(
        send_message.session_runtime_spaces, "enqueue", lambda *_a, **_k: pytest.fail("enqueued")
    )
    base_registry = configuration.build_agent_registry()
    assert base_registry.resolve("system", "main_agent").agent_type is AgentProfileType.MAIN
    monkeypatch.setattr(configuration, "get_agent_registry", lambda: base_registry)

    tool = send_message.SendMessageTool()
    context = ToolExecutionContext(
        step_id="step-1",
        session_id=7,
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

    fork_session = SimpleNamespace(
        id=41,
        title="前端部分",
        derived_by=lambda session_id: session_id == 7,
        session_type="agent_fork",
        workspace_id=3,
        current_run_id=11,
    )
    parent_session = SimpleNamespace(id=7, title="主对话", session_type="user", workspace_id=3)
    monkeypatch.setattr(
        send_message,
        "get_session_service",
        lambda: SimpleNamespace(
            get_session=lambda session_id: fork_session if session_id == 41 else parent_session,
            require_current_run=lambda _: run,
        ),
    )
    monkeypatch.setattr(send_message, "get_workspace_service", lambda: None)
    run = SimpleNamespace(id=11, status="completed", agent_id="main_agent", model_config_id="test/m3",
                          reasoning_effort="high")
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
        send_message.session_runtime_spaces,
        "enqueue",
        lambda session_id, message: enqueued.append((session_id, message)),
    )

    context = ToolExecutionContext(
        step_id="step-1",
        session_id=7,
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
    assert (kwargs["agent_id"], kwargs["model_config_id"]) == ("main_agent", "test/m3")
    assert kwargs["reasoning_effort"] == "high"
    assert enqueued == [(41, "agent-message")]
