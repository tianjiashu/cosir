"""运行时通知的不可变性与消息边界契约。"""

from dataclasses import FrozenInstanceError

import pytest

from app.models.runtime_notice import RuntimeNotice


def test_notice_freezes_metadata_and_keeps_routing_out_of_message_kwargs() -> None:
    metadata: dict[str, str | None] = {"source": "global", "scope": None}
    notice = RuntimeNotice(
        content="规则变化", kind="system_prompt_delta", run_id=7, metadata=metadata
    )
    metadata["source"] = "changed"
    assert notice.metadata == {"source": "global", "scope": None}
    with pytest.raises(FrozenInstanceError):
        notice.content = "changed"
    with pytest.raises(TypeError):
        notice.metadata["source"] = "changed"
    # 投递边界（kind / run_id 路由信息）由模型节点消费时据此判断，不在此处落地为消息字段。
    assert notice.content == "规则变化"
    assert notice.to_hint_text() == "规则变化"


@pytest.mark.parametrize("banned", [[], ["read_file"], ["read_file", "write_file"]])
def test_runner_tool_availability_is_a_run_scoped_notice(monkeypatch, banned) -> None:
    """工具准入的三条分支都生产领域通知，不在 runner 构造模型消息。"""
    from types import SimpleNamespace

    from app.core.runtime import runner as runner_module

    notices: list[RuntimeNotice] = []
    space = SimpleNamespace(
        task_tool_schemas=({"name": "read_file"}, {"name": "write_file"}),
        defer_runtime_notice=notices.append,
    )
    monkeypatch.setattr(runner_module.task_runtime_spaces, "get_or_create", lambda _id: space)
    monkeypatch.setattr(
        runner_module, "WorkflowOperations", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    registry = object()
    terminal_sessions = object()
    monkeypatch.setattr(runner_module, "get_agent_registry", lambda: registry)
    monkeypatch.setattr(runner_module, "get_terminal_session_service", lambda: terminal_sessions)
    runtime = runner_module.AgentRuntime.__new__(runner_module.AgentRuntime)
    runtime._tool_executor = object()
    runtime._process_tool_output_channel_factory = None
    result = runtime._build_operations(
        workspace=SimpleNamespace(id=13, root_path="/workspace"),
        task=SimpleNamespace(id=1),
        run=SimpleNamespace(id=7, extra=SimpleNamespace(ban_tools=banned)),
        agent_profile=SimpleNamespace(),
    )
    assert result.execution_context.task_id == 1
    assert result.execution_context.run_id == 7
    assert result.execution_context.workspace_id == 13
    assert str(result.execution_context.workspace_root) == "/workspace"
    assert result.runtime_dependencies.agent_profile_registry is registry
    assert result.runtime_dependencies.terminal_session_service is terminal_sessions
    assert result.allows_tools == {"read_file", "write_file"} - set(banned)
    assert len(notices) == 1
    assert isinstance(notices[0], RuntimeNotice)
    assert notices[0].kind == "tool_availability"
    assert notices[0].run_id == 7
