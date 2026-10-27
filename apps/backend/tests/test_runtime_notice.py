"""Run 内通知的投递契约：只进本次执行的 mailbox，不落库、不需要确认。"""

from types import SimpleNamespace

import pytest

from app.core.workflow.inbox.run_mailbox import RunMailbox
from app.core.workflow.inbox.task_message import NoticeBody, TaskMessage


@pytest.mark.parametrize("banned", [[], ["read_file"], ["read_file", "write_file"]])
async def test_tool_availability_is_a_run_scoped_notice(monkeypatch, banned) -> None:
    """工具准入的三条分支都向 mailbox 投递领域通知，不在装配方构造模型消息。"""
    from app.core.workflow.execution_state import runtime_context as runtime_context_module

    space = SimpleNamespace(task_tool_schemas=({"name": "read_file"}, {"name": "write_file"}))
    monkeypatch.setattr(
        runtime_context_module.task_runtime_spaces, "get_or_create", lambda _id: space
    )
    agent = SimpleNamespace()
    mailbox = RunMailbox()
    result = runtime_context_module.RuntimeContext.for_run(
        SimpleNamespace(id=7, options=SimpleNamespace(ban_tools=banned)),
        SimpleNamespace(id=1),
        SimpleNamespace(id=13, root_path="/workspace"),
        agent,
        mailbox,
        event_sink=print,
    )
    assert result.agent is agent
    assert result.mailbox is mailbox
    assert result.allows_tools == {"read_file", "write_file"} - set(banned)
    notice = mailbox.pop()
    assert mailbox.isEmpty()
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

    assert [mailbox.pop() for _ in range(3)] == [persisted, transient, transient]
    assert mailbox.isEmpty()
    # 取光后同一行再次投递仍被忽略：它已在本次执行中处理过；新执行开始时 reset 才重新接收。
    mailbox.post(persisted)
    assert mailbox.isEmpty()
    mailbox.reset()
    mailbox.post(persisted)
    assert mailbox.pop() == persisted
