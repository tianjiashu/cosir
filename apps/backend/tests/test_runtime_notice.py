"""Run 内通知的投递契约：只进本次执行的 mailbox，不落库、不需要确认。"""

from types import SimpleNamespace

import pytest

from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.models.task_message import NoticeBody, TaskMessage


@pytest.mark.parametrize("banned", [[], ["read_file"], ["read_file", "write_file"]])
async def test_tool_availability_is_a_run_scoped_notice(monkeypatch, banned) -> None:
    """工具准入的三条分支都向 mailbox 投递领域通知，不在装配方构造模型消息。"""
    from app.service.conversation_run import run_driver

    space = SimpleNamespace(task_tool_schemas=({"name": "read_file"}, {"name": "write_file"}))
    monkeypatch.setattr(run_driver.task_runtime_spaces, "get_or_create", lambda _id: space)
    monkeypatch.setattr(
        run_driver, "RuntimeContext", lambda **kwargs: SimpleNamespace(**kwargs)
    )
    monkeypatch.setattr(
        run_driver, "get_conversation_event_projector", lambda: SimpleNamespace(process=print)
    )
    agent = SimpleNamespace()
    mailbox = RunMailbox()
    result = run_driver._build_context(
        workspace=SimpleNamespace(id=13, root_path="/workspace"),
        task=SimpleNamespace(id=1),
        run=SimpleNamespace(id=7, options=SimpleNamespace(ban_tools=banned)),
        agent=agent,
        mailbox=mailbox,
    )
    assert result.agent is agent
    assert result.mailbox is mailbox
    assert result.allows_tools == {"read_file", "write_file"} - set(banned)
    [notice] = mailbox.drain()
    assert isinstance(notice.body, NoticeBody)
    assert notice.id is None
    assert notice.body.notice_kind == "tool_availability"


def test_mailbox_dedupes_persisted_rows_but_keeps_every_run_notice() -> None:
    """同一外部队列行在一次执行内只收一次；Run 内通知没有行 id，逐条保留。"""
    mailbox = RunMailbox()
    persisted = TaskMessage.of(NoticeBody(content="broadcast", notice_kind="test"), message_id=5)
    transient = TaskMessage.of(NoticeBody(content="continue", notice_kind="test"))

    mailbox.post(persisted)
    mailbox.post(persisted)
    mailbox.post(transient)
    mailbox.post(transient)

    assert mailbox.drain() == [persisted, transient, transient]
    assert mailbox.drain() == []
    # 取光后同一行再次投递仍被忽略：它已在本次执行中处理过。
    mailbox.post(persisted)
    assert mailbox.drain() == []
