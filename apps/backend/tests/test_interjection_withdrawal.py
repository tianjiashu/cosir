"""撤回还没并入的插话：真实 actor / workflow / SQLite / 发送与删除服务，只替换模型流与终端执行器。

潜在缺陷：mailbox 里的副本在用户删除后仍在，同批没开始的调用照样让位，模型收到「新用户消息取代了本批
工具」却找不到这条消息；让位已经发生后再删，模型同样对着一条不存在的消息。
"""

import asyncio

from app.assistant_transport.request.session_command_request import SendMessageRequest
from app.assistant_transport.service.assistant_transport_service import AssistantTransportService
from app.service import depends
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces as spaces
from tests.live_cold_support import GatedCommands, LiveColdEnv, tool_call, tool_parts


def _command(name: str) -> dict[str, object]:
    return {"intent": name, "command": name}


def _batch() -> tuple[list[dict[str, object]], str]:
    return (
        [{"tool_calls": [
            tool_call("c1", "execute_terminal", _command("A"), 0),
            tool_call("c2", "execute_terminal", _command("B"), 1),
        ]}],
        "tool_calls",
    )


async def _interject(env: LiveColdEnv, service: AssistantTransportService, commands: GatedCommands) -> int:
    """A 运行中发一条插话，等它投进 mailbox，返回它的消息 id。"""
    async with asyncio.timeout(8):
        while commands.started != ["A"]:  # noqa: ASYNC110 - 闸门命令与 mailbox 没有可等的事件
            await asyncio.sleep(0.02)
    request = SendMessageRequest.model_validate({
        "message": {"role": "user", "parts": [{"type": "text", "text": "插话"}]},
        "modelConfigId": env.model_config.id,
        "clientMessageId": "client-1",
    })
    await service.send(env.session.id, request)
    mailbox = spaces.get(env.session.id).actor.mailbox
    async with asyncio.timeout(4):
        while not mailbox.user_input_ids():  # noqa: ASYNC110 - 同上
            await asyncio.sleep(0.02)
    [message_id] = mailbox.user_input_ids()
    return message_id


def _texts(messages: list[dict[str, object]]) -> str:
    return "\n".join(str(message.get("content")) for message in messages)


async def test_deleted_before_anything_yielded_is_as_if_never_sent(backend_env, tmp_path, monkeypatch):
    commands = GatedCommands(monkeypatch, "A", "B")
    env = LiveColdEnv(tmp_path, monkeypatch, [_batch(), ([{"content": "完成"}], "stop")])
    service = AssistantTransportService()
    try:
        env.send()
        message_id = await _interject(env, service, commands)
        await service.delete_queued(env.session.id, message_id)
        assert not spaces.get(env.session.id).actor.mailbox.user_input_ids()
        commands.release()
        assert (await env.until("completed")).final_output == "完成"
        # 同批的 B 照常执行；模型看不到插话，也看不到撤回通知。
        assert commands.started == ["A", "B"]
        assert [part["status"] for part in tool_parts(env.cold())] == ["completed", "completed"]
        seen = _texts(env.model.requests[1])
        assert "插话" not in seen and "withdrew" not in seen and "superseded" not in seen
        assert depends.get_session_message_queue().pending_nowait(env.session.id) == []
        env.assert_parity()
    finally:
        commands.release()
        await env.close()


async def test_deleted_after_it_stopped_work_tells_the_model(backend_env, tmp_path, monkeypatch):
    commands = GatedCommands(monkeypatch, "A", "B")
    env = LiveColdEnv(tmp_path, monkeypatch, [_batch(), ([{"content": "继续"}], "stop")])
    service = AssistantTransportService()
    try:
        env.send()
        message_id = await _interject(env, service, commands)
        # 用户随即停止：A 被停下，B 为插话让位，插话留在队列里（停止前排着的不自动接续）。
        await service.cancel(env.session.id)
        await env.until("cancelled")
        assert commands.started == ["A"]
        statuses = {part["toolCallId"]: part["status"] for part in tool_parts(env.cold())}
        assert statuses["c2"] == "cancelled"
        [queued] = depends.get_session_message_queue().pending_nowait(env.session.id)
        assert queued.id == message_id and queued.interrupted_work

        await service.delete_queued(env.session.id, message_id)
        [notice] = depends.get_session_message_queue().pending_nowait(env.session.id)
        assert notice.kind == "notice" and notice.body.notice_kind == "input_withdrawn"

        env.send("下一步")
        assert (await env.until("completed")).final_output == "继续"
        seen = _texts(env.model.requests[-1])
        assert "withdrew a message" in seen and "插话" not in seen
        # 撤回通知不投影：实时快照与冷重建一致。
        env.assert_parity()
    finally:
        commands.release()
        await env.close()
