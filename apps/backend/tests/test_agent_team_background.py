"""真实审批、节点调度与结果回投；模型输出由测试提供。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.api.schemas.AgentConfigurationDocument import AgentConfigurationDocument
from app.assistant_transport.request import DecisionRequest
from app.assistant_transport.service.transport_assistant_service import TransportAssistantService
from app.core.hook import hook_registry
from app.core.task_actor import SessionConflictError
from app.core.tool.background.backgraound_registry import background_registry
from app.core.workflow import workflow
from app.core.workflow.branch import task_title
from app.core.workflow.message_queue.task_message import RunLaunch, TaskMessage, UserInputBody
from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.models.conversation_input import UserInputPresentation
from app.models.message_metadata import UserMessageMetadata
from app.service import depends
from app.service.configuration.agent_configuration_service import AgentConfigurationService
from app.service.configuration.agent_team_configuration_registry import get_agent_team_registry
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces as spaces
from tests.sdk_fixtures import sdk_completion, sdk_events

CALL_ID = "team-review"
GOAL = {"goal": "review changes", "summary": "Review"}


class ReviewModel:
    def __init__(self):
        self.proposed = False
        self.node_started = asyncio.Event()
        self.receipt_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.handoff = '{"status":"done","output":"reviewed"}'

    async def stream(self, messages):
        inputs = [str(m.get("content")) for m in messages if m["role"] == "user"]
        if any("<agent_message " in text for text in inputs):
            self.node_started.set()
            await self.release.wait()
            frames = sdk_events([{"content": self.handoff}])
        elif not self.proposed:
            self.proposed = True
            args = {
                "intent": "启动审查团队", "team_id": "review", "goal": "review",
                "node_goals": [{"node_id": node_id, **GOAL} for node_id in ("a", "b")],
            }
            frames = sdk_events([{"tool_calls": [{
                "index": 0, "id": CALL_ID, "type": "function",
                "function": {"name": "agent_team", "arguments": json.dumps(args)},
            }]}], finish_reason="tool_calls")
        else:
            if any("<tool_result " in text for text in inputs):
                self.receipt_seen.set()
            frames = sdk_events([{"content": "parent answer"}])
        for frame in frames:
            yield frame

    async def complete(self, _messages, *, response_format):
        return sdk_completion(self.handoff)

    async def close(self):
        pass


def _model_config(name):
    return depends.get_model_config_service().create_config(
        config_name=name, base_url="http://127.0.0.1", api_key="test", model_name="test",
        context_window_k=96, supports_thinking=True, supports_reasoning_effort=True,
        supports_image=False,
    )


def _send(task_id, model_id, text):
    spaces.enqueue(task_id, TaskMessage.of(UserInputBody(
        input_text=text, launch=RunLaunch(model_config_id=model_id),
        presentation=UserInputPresentation(display_text=text),
    )))


async def _parked(actor):
    while True:
        changed = actor.state_changed
        run = depends.get_task_service().require_current_run(actor.task_id)
        if run and run.status != "running" and not actor.driving:
            return run
        await changed.wait()


@pytest.fixture
def reviewer(backend_env, tmp_path):
    agents = AgentConfigurationService(workspace_root=tmp_path)
    document = AgentConfigurationDocument(
        agent_id="second-reviewer", role="review", description="review changes",
        system_prompt="Review the task", allowed_tools=["read_file"],
    )
    agents.create_document(document)
    return agents, document


@pytest.fixture
async def proposed_team(reviewer, tmp_path, monkeypatch):
    """主 Task 已收到 A → B 的待批方案；各用例经真实决定入口继续。"""
    model_config = _model_config("proposer")
    ws = depends.get_workspace_service().create_workspace("ws", str(tmp_path))
    parent = depends.get_workspace_service().create_task(ws.id, "parent")
    _, document = reviewer
    config = AgentTeamConfiguration.model_validate({
        "team_id": "review", "name": "Review", "description": "Review changes",
        "start_node_id": "a",
        "nodes": [
            {"node_id": key, "name": key.upper(), "agent_id": agent_id,
             "statuses": [{"name": "done", "color": "#16A34A"}]}
            for key, agent_id in [("a", "general-assistant"), ("b", document.agent_id)]
        ],
        "transitions": [
            {"from_node_id": "a", "status": "done", "target_node_id": "b"},
            {"from_node_id": "b", "status": "done", "target_node_id": "END"},
        ],
    })
    get_agent_team_registry().register(config, workspace_root=tmp_path)
    model = ReviewModel()
    monkeypatch.setattr(hook_registry, "_registry", hook_registry.HookRegistry())
    monkeypatch.setattr(workflow, "resolve_chat_model",
                        lambda **_: SimpleNamespace(model=model, supports_thinking=False))
    monkeypatch.setattr(task_title, "should_launch", lambda *_: False)
    spaces.bind_loop(asyncio.get_running_loop())
    try:
        _send(parent.id, model_config.id, "use team")
        first = await asyncio.wait_for(_parked(spaces.get(parent.id).actor), 5)
        assert first.status == "suspended"
        decision = DecisionRequest(decision="approve", data={
            "configuration": config.model_dump(mode="json"), "goal": "review",
            "node_goals": {node.node_id: GOAL for node in config.nodes},
        })
        yield first, decision, model
    finally:
        model.release.set()
        await asyncio.to_thread(background_registry.close)
        await depends.get_agent_team_coordinator().aclose()
        await spaces.aclose()


@pytest.mark.parametrize(
    "ending", ["done", "failed", "user_stop", "parent_stop", "delete", "shutdown"],
)
async def test_approved_team_runs_in_background(proposed_team, ending):
    first, decision, model = proposed_team
    service = TransportAssistantService()
    task_id = first.task_id
    actor = spaces.get(task_id).actor
    coordinator = depends.get_agent_team_coordinator()
    async with asyncio.timeout(12):
        if ending == "done":
            # 结束提案 Run，再用另一模型开一轮。之后的批准 Run 继承新模型，节点仍按提案解析。
            _send(task_id, first.model_config_id, "finish this turn")
            assert await actor.wait_quiescent(5)
            _send(task_id, _model_config("later").id, "another request")
            assert await actor.wait_quiescent(5)
        elif ending == "failed":
            model.handoff = "invalid handoff"
        await service.decide(task_id, CALL_ID, decision)
        await model.receipt_seen.wait()
        await model.node_started.wait()
        owner = await _parked(actor)
        assert owner.status == "suspended"
        assert (owner.id != first.id) == (ending == "done")
        if ending == "done":
            assert owner.model_config_id != first.model_config_id
            row = coordinator._team_run_crud.get_by_tool_call(task_id, CALL_ID)
            assert row.state_json["node_runtime"]["a"]["model_config_id"] == first.model_config_id
        rows = service.get_state(task_id)["background_tasks"]
        assert [(r["kind"], r["label"], r["status"]) for r in rows] == [
            ("agent_team", "Review", "running")
        ]
        with pytest.raises(SessionConflictError) as caught:
            await service.decide(task_id, CALL_ID, decision)
        assert caught.value.code == "DECISION_NOT_AWAITED"
        if ending == "user_stop":
            assert await asyncio.to_thread(service.cancel_tool_call, task_id, CALL_ID)
        elif ending == "parent_stop":
            await actor.cancel()
        elif ending in {"delete", "shutdown"}:
            if ending == "shutdown":
                # 模型一直不发下一帧，协调者仍应当退出。
                await asyncio.to_thread(background_registry.close)
                await coordinator.aclose()
            else:
                operation = asyncio.create_task(
                    depends.get_task_service().delete_task_tree(task_id)
                )
                model.release.set()
                await operation
            assert not coordinator._active
            return
        model.release.set()
        assert await actor.wait_quiescent(5)
        if ending == "parent_stop":
            await asyncio.gather(*(task for _, task in coordinator._active.values()))
        runs = depends.get_conversation_run_crud().list_by_task(task_id)
        assert len(runs) == (3 if ending == "done" else 1)
        current = depends.get_task_service().require_current_run(task_id)
        records = depends.get_conversation_task_context_crud().get(task_id)
        results = [
            r for r in records
            if isinstance(r.metadata, UserMessageMetadata)
            and r.metadata.kind == "background_result"
        ]
        [placeholder] = [r for r in records if r.message.get("tool_call_id") == CALL_ID]
        assert placeholder.run_id == first.id
        assert placeholder.metadata.background.run_id == owner.id
        assert placeholder.metadata.display_data["kind"] == "agent-team-approval"
        state = service.get_state(task_id)["background_tasks"][0]
        if ending == "parent_stop":
            assert current.status == "cancelled" and results == []
            assert state["status"] == "cancelled"
        else:
            assert current.status == "completed"
            [result] = results
            body = str(result.message["content"])
            expected = {"user_stop": "cancelled", "done": "completed", "failed": "failed"}[ending]
            assert f'"status": "{expected}"' in body and ".cosir/agent-team-runs/" in body
            assert state["status"] == ("cancelled" if ending == "user_stop" else "completed")


async def test_approval_with_deleted_agent_fails_team_and_card(proposed_team, reviewer):
    first, decision, _ = proposed_team
    agents, document = reviewer
    agents.delete_document(document.agent_id)
    service = TransportAssistantService()
    await service.decide(first.task_id, CALL_ID, decision)
    assert await spaces.get(first.task_id).actor.wait_quiescent(5)

    coordinator = depends.get_agent_team_coordinator()
    row = coordinator._team_run_crud.get_by_tool_call(first.task_id, CALL_ID)
    assert (row.status, row.end_reason, row.started_at) == ("failed", "team_start_failed", None)
    records = depends.get_conversation_task_context_crud().get(first.task_id)
    [card] = [r.metadata for r in records if r.message.get("tool_call_id") == CALL_ID]
    assert (card.status, card.error, card.team_run_id) == ("failed", "方案配置无效", row.id)
    [receipt] = [
        r for r in records
        if isinstance(r.metadata, UserMessageMetadata) and r.metadata.kind == "tool_result"
    ]
    assert document.agent_id in receipt.message["content"]
    with pytest.raises(SessionConflictError) as rejected:
        await service.decide(first.task_id, CALL_ID, decision)
    assert rejected.value.code == "DECISION_NOT_AWAITED"


async def test_failed_node_input_does_not_start_another_run_after_restart(proposed_team, reviewer):
    first, decision, model = proposed_team
    agents, document = reviewer
    coordinator = depends.get_agent_team_coordinator()
    async with asyncio.timeout(12):
        await TransportAssistantService().decide(first.task_id, CALL_ID, decision)
        await model.node_started.wait()
        # A 正在请求模型；删除 B 的配置后，B 的 Run 能建立，但在并入输入前失败。
        agents.delete_document(document.agent_id)
        model.release.set()
        assert await spaces.get(first.task_id).actor.wait_quiescent(5)
        row = coordinator._team_run_crud.get_by_tool_call(first.task_id, CALL_ID)
        assert (row.status, row.end_reason) == ("failed", "node_run_failed")
        node_id = row.state_json["node_executions"]["b"]["task_id"]
        runs = depends.get_conversation_run_crud()
        [failed] = runs.list_by_task(node_id)
        assert failed.status == "failed"

        await asyncio.to_thread(background_registry.close)
        await coordinator.aclose()
        await spaces.aclose()
        agents.create_document(document)
        spaces.bind_loop(asyncio.get_running_loop())
        background_registry.open()
        depends.get_recover_service().recover_after_restart()
        assert await spaces.get_or_create(node_id).actor.wait_quiescent(5)
        assert [r.id for r in runs.list_by_task(node_id)] == [failed.id]
