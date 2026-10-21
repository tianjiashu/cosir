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
async def test_tool_availability_is_a_run_scoped_notice(monkeypatch, banned) -> None:
    """工具准入的三条分支都生产领域通知，不在装配方构造模型消息。"""
    from types import SimpleNamespace

    from app.service.conversation_run import run_driver

    notices: list[RuntimeNotice] = []
    space = SimpleNamespace(
        task_tool_schemas=({"name": "read_file"}, {"name": "write_file"}),
        defer_runtime_notice=notices.append,
    )
    monkeypatch.setattr(run_driver.task_runtime_spaces, "get_or_create", lambda _id: space)
    monkeypatch.setattr(
        run_driver, "RuntimeContext", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    monkeypatch.setattr(
        run_driver, "get_conversation_event_projector", lambda: SimpleNamespace(process=print)
    )
    agent = SimpleNamespace()
    result = run_driver._build_context(
        workspace=SimpleNamespace(id=13, root_path="/workspace"),
        task=SimpleNamespace(id=1),
        run=SimpleNamespace(id=7, options=SimpleNamespace(ban_tools=banned)),
        agent=agent,
    )
    assert result.agent is agent
    assert result.allows_tools == {"read_file", "write_file"} - set(banned)
    assert len(notices) == 1
    assert isinstance(notices[0], RuntimeNotice)
    assert notices[0].kind == "tool_availability"
    assert notices[0].run_id == 7
