"""一次驱动的 mailbox 只属于这次驱动：收尾即清空，残留的投递副本不能误导驱动之外的读取方。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core import session_execution as execution_module
from app.core.session_execution import SessionExecution
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.session_message import (
    RunLaunch,
    SessionMessage,
    UserInputBody,
)
from app.models.message_context.message_metadata.user_message_metadata import UserInputPresentation


async def test_released_drive_leaves_no_user_input_in_the_mailbox(monkeypatch) -> None:
    """停止前刚投进、还没并入的用户输入仍在外部队列；mailbox 里的副本随驱动收尾丢弃，
    否则空闲期间子 Agent 申请批准会被判成「页面上有新输入」而一直让位。"""
    gate = asyncio.Event()

    class Driver:
        async def drive(self, _run, _preceding):
            await gate.wait()

    monkeypatch.setattr(execution_module, "RunDriver", Driver)
    released = asyncio.Event()

    async def on_released(_run, _reason):
        released.set()

    mailbox = RunMailbox()
    execution = SessionExecution(9, mailbox, on_released)
    execution.start(SimpleNamespace(id=1), [], None)
    body = UserInputBody(
        text="stop that",
        launch=RunLaunch(model_config_id="test/m1"),
        presentation=UserInputPresentation(text="stop that"),
    )
    mailbox.post(SessionMessage.of(body, message_id=5))
    assert mailbox.user_input_ids()
    gate.set()
    await asyncio.wait_for(released.wait(), timeout=2)
    assert not mailbox.user_input_ids()
