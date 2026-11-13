"""真实审批、节点调度与 Team 落定后的汇报；模型输出由测试提供。

Team 是多 Agent 编排：批准即开跑，不登记后台工作；落定后以 TeamRun 为来源的 Agent 消息汇报给主 Task。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.assistant_transport.request import DecisionRequest
from app.assistant_transport.service.assistant_transport_service import AssistantTransportService
from app.core.workflow import workflow
from app.core.workflow.branch import task_title
from app.core.workflow.message_queue.task_message import RunLaunch, TaskMessage, UserInputBody
from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.models.conversation_input import UserInputPresentation
from app.models.errors.task_command_errors import TaskCommandConflictError
from app.models.message_metadata import UserMessageMetadata
from app.service import depends
from app.service.configuration.agent_configuration import AgentConfiguration
from app.service.configuration.agent_configuration_service import AgentConfigurationService
from app.service.configuration.agent_team_configuration_registry import get_agent_team_registry
from app.storage.queries.task_status_queries import TaskStatus, TaskStatusQueries
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces as spaces
from app.tools.tool_handler.terminal.command_results import command_results
from tests.model_config_fixtures import create_model_config
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
        # 节点输入带子目标；主 Task 收到的 Team 汇报也是 Agent 消息，靠这一句区分。
        if any("<agent_message " in text and "Current node subgoal" in text for text in inputs):
            self.node_started.set()
            await self.release.wait()
            frames = sdk_events([{"content": self.handoff}])
        elif not self.proposed:
            self.proposed = True
            args = {
                "intent": "启动审查团队",
                "team_id": "review",
                "goal": "review",
                "node_goals": [{"node_id": node_id, **GOAL} for node_id in ("a", "b")],
            }
            frames = sdk_events(
                [
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": CALL_ID,
                                "type": "function",
                                "function": {"name": "agent_team", "arguments": json.dumps(args)},
                            }
                        ]
                    }
                ],
                finish_reason="tool_calls",
            )
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
    return create_model_config(
        name=name,
        base_url="http://127.0.0.1",
        api_key="test",
        model_name="test",
        context_window_k=96,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_image=False,
    )


def _send(task_id, model_id, text):
    spaces.enqueue(
        task_id,
        TaskMessage.of(
            UserInputBody(
                text=text,
                launch=RunLaunch(model_config_id=model_id),
                presentation=UserInputPresentation(text=text),
            )
        ),
    )


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
    document = AgentConfiguration(
        agent_id="second-reviewer",
        role="review",
        description="review changes",
        system_prompt="Review the task",
        allowed_tools=["read_file"],
    )
    agents.create(document)
    return agents, document


@pytest.fixture
async def proposed_team(reviewer, tmp_path, monkeypatch):
    """主 Task 已收到 A → B 的待批方案；各用例经真实决定入口继续。"""
    model_config = _model_config("proposer")
    ws = depends.get_workspace_service().create_workspace("ws", str(tmp_path))
    parent = depends.get_workspace_service().create_task(ws.id, "parent")
    _, document = reviewer
    config = AgentTeamConfiguration.model_validate(
        {
            "team_id": "review",
            "name": "Review",
            "description": "Review changes",
            "start_node_id": "a",
            "nodes": [
                {
                    "node_id": key,
                    "name": key.upper(),
                    "agent_id": agent_id,
                    "statuses": [{"name": "done", "color": "#16A34A"}],
                }
                for key, agent_id in [("a", "general-assistant"), ("b", document.agent_id)]
            ],
            "transitions": [
                {"from_node_id": "a", "status": "done", "target_node_id": "b"},
                {"from_node_id": "b", "status": "done", "target_node_id": "END"},
            ],
        }
    )
    get_agent_team_registry().register(config, workspace_root=tmp_path)
    model = ReviewModel()
    monkeypatch.setattr(
        workflow,
        "resolve_chat_model",
        lambda **_: SimpleNamespace(model=model, supports_thinking=False),
    )
    monkeypatch.setattr(task_title, "should_launch", lambda *_: False)
    spaces.bind_loop(asyncio.get_running_loop())
    try:
        _send(parent.id, model_config.id, "use team")
        first = await asyncio.wait_for(_parked(spaces.get(parent.id).actor), 5)
        assert first.status == "suspended"
        decision = DecisionRequest(
            decision="approve",
            data={
                "configuration": config.model_dump(mode="json"),
                "goal": "review",
                "node_goals": {node.node_id: GOAL for node in config.nodes},
            },
        )
        yield first, decision, model
    finally:
        model.release.set()
        await asyncio.to_thread(command_results.close)
        await depends.get_agent_team_coordinator().aclose()
        await spaces.aclose()


@pytest.mark.parametrize(
    "ending",
    ["done", "failed", "user_stop", "task_stop", "parent_stop", "delete", "shutdown"],
)
async def test_approved_team_reports_to_the_main_task(proposed_team, ending):
    first, decision, model = proposed_team
    service = AssistantTransportService()
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
        # 批准的 Run 读到批准结果就答复并完成，Team 照常运行；它不是后台工作，看板上没有它。
        owner = await _parked(actor)
        assert owner.status == "completed"
        assert (owner.id != first.id) == (ending == "done")
        row = coordinator._team_run_crud.get_by_tool_call(task_id, CALL_ID)
        if ending == "done":
            assert owner.model_config_id != first.model_config_id
            assert row.state_json["node_runtime"]["a"]["model_config_id"] == first.model_config_id
        assert service.get_state(task_id)["background_tasks"] == []
        with pytest.raises(TaskCommandConflictError) as caught:
            await service.decide(task_id, CALL_ID, decision)
        assert caught.value.code == "DECISION_NOT_AWAITED"
        if ending == "user_stop":
            assert coordinator.cancel_by_user(row.id)
        elif ending == "task_stop":
            # 用户停止 Task 只打断当前驱动：Task 空闲时什么也不做，Team 照常跑完并汇报。
            await service.cancel(task_id)
        elif ending == "parent_stop":
            # 上游收回主 Task：Team 一并停下，不汇报。
            await actor.cancel("parent")
        elif ending in {"delete", "shutdown"}:
            if ending == "shutdown":
                # 模型一直不发下一帧，协调者仍应当退出。
                await asyncio.to_thread(command_results.close)
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
        await asyncio.gather(*(task for _, task in coordinator._active.values()))
        # 主 Task 只在 Team 落定、汇报处理完之后才算无事可做。
        assert await actor.wait_quiescent(5)
        runs = depends.get_conversation_run_crud().list_by_task(task_id)
        # 汇报到达时主 Task 空闲，开出新 Run 收尾；被停下（单停、收回）的 Team 不开新 Run。
        stopped = ending in {"user_stop", "parent_stop"}
        assert len(runs) == (4 if ending == "done" else 1 if stopped else 2)
        records = depends.get_conversation_task_context_crud().get(task_id)
        reports = [
            r
            for r in records
            if isinstance(r.metadata, UserMessageMetadata)
            and r.metadata.source is not None
            and r.metadata.source.team_run_id == row.id
        ]
        [placeholder] = [r for r in records if r.message.get("tool_call_id") == CALL_ID]
        assert placeholder.run_id == first.id
        assert placeholder.metadata.background is None
        assert placeholder.metadata.display_data["kind"] == "agent-team-approval"
        queued = [m.kind for m in depends.get_task_message_queue().pending_nowait(task_id)]
        if stopped:
            assert reports == []
            # 单停只留一条不唤醒的通知，等下一次推进告诉 Agent；收回什么也不留。
            assert queued == (["system_notice"] if ending == "user_stop" else [])
        else:
            [report] = reports
            expected = {"done": "completed", "failed": "failed", "task_stop": "completed"}[ending]
            assert report.metadata.source.report.status == expected
            body = str(report.message["content"])
            assert f'source_team_run_id="{row.id}"' in body and 'report="final"' in body
            assert f'"status": "{expected}"' in body
            assert ".cosir/agent-team-runs/" in body
            assert queued == []


async def test_approval_with_deleted_agent_fails_team_and_card(proposed_team, reviewer):
    first, decision, _ = proposed_team
    agents, document = reviewer
    agents.delete(document.agent_id)
    service = AssistantTransportService()
    await service.decide(first.task_id, CALL_ID, decision)
    assert await spaces.get(first.task_id).actor.wait_quiescent(5)

    coordinator = depends.get_agent_team_coordinator()
    row = coordinator._team_run_crud.get_by_tool_call(first.task_id, CALL_ID)
    assert (row.status, row.end_reason, row.started_at) == ("failed", "team_start_failed", None)
    records = depends.get_conversation_task_context_crud().get(first.task_id)
    [card] = [r.metadata for r in records if r.message.get("tool_call_id") == CALL_ID]
    assert (card.status, card.error, card.team_run_id) == ("failed", "方案配置无效", row.id)
    [receipt] = [
        r
        for r in records
        if isinstance(r.metadata, UserMessageMetadata) and r.metadata.kind == "tool_result"
    ]
    assert document.agent_id in receipt.message["content"]
    with pytest.raises(TaskCommandConflictError) as rejected:
        await service.decide(first.task_id, CALL_ID, decision)
    assert rejected.value.code == "DECISION_NOT_AWAITED"


async def test_failed_node_input_does_not_start_another_run_after_restart(proposed_team, reviewer):
    first, decision, model = proposed_team
    agents, document = reviewer
    coordinator = depends.get_agent_team_coordinator()
    async with asyncio.timeout(12):
        await AssistantTransportService().decide(first.task_id, CALL_ID, decision)
        await model.node_started.wait()
        # A 正在请求模型；删除 B 的配置后，B 的 Run 能建立，但在并入输入前失败。
        agents.delete(document.agent_id)
        model.release.set()
        assert await spaces.get(first.task_id).actor.wait_quiescent(5)
        row = coordinator._team_run_crud.get_by_tool_call(first.task_id, CALL_ID)
        assert (row.status, row.end_reason) == ("failed", "node_run_failed")
        node_id = row.state_json["node_executions"]["b"]["task_id"]
        runs = depends.get_conversation_run_crud()
        [failed] = runs.list_by_task(node_id)
        assert failed.status == "failed"

        await asyncio.to_thread(command_results.close)
        await coordinator.aclose()
        await spaces.aclose()
        agents.create(document)
        spaces.bind_loop(asyncio.get_running_loop())
        command_results.open()
        depends.get_recover_service().recover_after_restart()
        assert await spaces.get_or_create(node_id).actor.wait_quiescent(5)
        assert [r.id for r in runs.list_by_task(node_id)] == [failed.id]


async def test_withdrawing_the_proposal_turn_stops_the_running_team_silently(proposed_team):
    """撤回提出方案的那一回合：运行中的 Team 停下、不汇报，主 Task 不会被它的结论再叫醒。"""
    first, decision, model = proposed_team
    service = AssistantTransportService()
    task_id = first.task_id
    actor = spaces.get(task_id).actor
    coordinator = depends.get_agent_team_coordinator()
    async with asyncio.timeout(12):
        await service.decide(task_id, CALL_ID, decision)
        await model.node_started.wait()
        await _parked(actor)
        target = depends.get_conversation_task_context_service().latest_user_input(task_id)
        await service.withdraw(task_id, target.message_id)
        model.release.set()
        await asyncio.gather(*(task for _, task in coordinator._active.values()))
        assert await actor.wait_quiescent(5)

    row = coordinator._team_run_crud.get_by_tool_call(task_id, CALL_ID)
    assert (row.status, row.end_reason) == ("cancelled", "cancelled")
    assert len(depends.get_conversation_run_crud().list_by_task(task_id)) == 1
    assert depends.get_task_message_queue().pending_nowait(task_id) == []


@pytest.mark.parametrize("resolve", ["decide", "withdraw"])
async def test_a_pending_proposal_awaits_the_user_whatever_the_run_does(proposed_team, resolve):
    """「等你操作」与 Run 正交：提案 Run 被停下后方案仍在等你；作出决定或撤回那一回合后清零，
    并经 Task 状态推送交给订阅者。"""
    first, decision, model = proposed_team
    service = AssistantTransportService()
    task_id = first.task_id
    actor = spaces.get(task_id).actor
    reads = TaskStatusQueries(main_session_factory())
    feed = depends.get_task_feed()
    subscription = feed.subscribe(asyncio.get_running_loop())
    try:
        async with asyncio.timeout(12):
            assert reads.status(task_id) == TaskStatus("suspended", 1)
            await service.cancel(task_id)
            assert (await _parked(actor)).status == "cancelled"
            assert reads.status(task_id) == TaskStatus("cancelled", 1)

            subscription.take()
            if resolve == "decide":
                await service.decide(task_id, CALL_ID, decision)
                await model.node_started.wait()
                await _parked(actor)
            else:
                target = depends.get_conversation_task_context_service().latest_user_input(task_id)
                await service.withdraw(task_id, target.message_id)
            await asyncio.sleep(0)
            assert reads.status(task_id).awaiting_user_calls == 0
            assert subscription.take().statuses[task_id].awaiting_user_calls == 0
    finally:
        feed.unsubscribe(subscription)
