"""实际 ReAct 节点、工具执行器与 SQLite 上下文；联网用例须显式启用。"""

import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from app.config import configuration
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.agents.model_settings import ModelSettings
from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
from app.core.events import AssistantTextDeltaEvent
from app.core.llm_provider import model_factory
from app.core.llm_provider.claude.codec import encode_messages
from app.core.llm_provider.claude.model import ClaudeChatModel
from app.core.llm_provider.model_probe import probe_chat_model
from app.core.tool.execute.tool_executor import ToolExecutor
from app.core.tool.schemas import ToolDefinition
from app.core.tool.tool_registry import ToolRegistry
from app.core.workflow.workflow import ReactLikeWorkflow
from app.models.enums.conversation_run_status import RunTransition
from app.models.message_metadata import UserMessageMetadata
from app.models.model_config_record import ModelConfigRecord
from tests.runtime_context_stub import run_state_stub, runtime_stub
from tests.test_claude_subscription import frames, token
from tests.claude_fixtures import sonnet_info


class SumArgs(BaseModel):
    a: int
    b: int


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "live",
    [
        False,
        pytest.param(
            True,
            marks=[
                pytest.mark.llm,
                pytest.mark.skipif(
                    os.environ.get("COSIR_CLAUDE_LIVE") != "1", reason="需要显式使用本机订阅额度"
                ),
            ],
        ),
    ],
)
async def test_cosir_react_stream_tool_and_persisted_native_context(
    canonical_store, monkeypatch, live
):
    store = canonical_store
    calls, events, completed, requests = [], [], [], []

    def local_sum(a, b, *, execution_context):
        calls.append((a, b, execution_context.tool_call_id))
        return str(a + b)

    definition = ToolDefinition(
        name="local_sum",
        group="test",
        description="Add two integers locally.",
        handler=local_sum,
        args_model=SumArgs,
    )
    registry = ToolRegistry([definition])
    config = ModelConfigRecord(
        provider="claude_subscription",
        config_name="Claude 测试",
        base_url="https://api.anthropic.com",
        api_key="",
        model_name="claude-sonnet-4-6",
        context_window_k=200,
        supports_thinking=True,
        supports_reasoning_effort=True,
    )
    agent = AgentRuntimeConfig.resolve(
        AgentProfile(
            agent_id="test_agent",
            role="测试",
            system_prompt="Follow the requested output format.",
            allowed_tools=["local_sum"],
            max_steps=3,
            model_settings=ModelSettings(max_tokens=2048, reasoning_effort="low"),
        ),
        config,
    )
    if live:
        # 设置页面的单 token 探活与正式 thinking/tool 请求都必须可用。
        outcome = await probe_chat_model(agent.model)
        assert outcome.ok, outcome
    run = store.runs.create(store.task.id, status="running")
    store.context.create_system_prompt(
        store.task.id,
        {
            "role": "system",
            "content": (
                "You are Cosir's local test agent. " "Use the provided local_sum tool when asked."
            ),
        },
    )
    manager = RuntimeMessageContextManager(store.task.id, agent, store.context, store.workspace)
    manager.begin_run(run)
    manager.add_message(
        {
            "role": "user",
            "content": (
                "Call local_sum exactly once with a=17 and b=25. "
                "After receiving its result, reply with exactly COSIR_TOOL_OK:42."
            ),
        },
        metadata=UserMessageMetadata(kind="notice"),
    )

    def transition(_run_id, transition, **kwargs):
        completed.append((transition, kwargs))
        return object()

    rc = runtime_stub(
        run=run,
        task=store.task,
        workspace=store.workspace,
        agent=agent,
        allows_tools=frozenset({"local_sum"}),
        event_sink=events.append,
        run_state=run_state_stub(transition=transition),
        task_space=SimpleNamespace(
            task_tool_schemas=[definition.to_model_tool_definition()],
            get_context_manager=lambda **_: manager,
        ),
    )
    # 工具执行器与四个业务节点均使用生产实现，替身只承担 Run 台账和网络测试边界。
    monkeypatch.setattr(configuration, "_TOOL_REGISTRY", registry)
    monkeypatch.setattr(configuration, "_TOOL_EXECUTOR", ToolExecutor(registry))
    if not live:

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=frames(tool=len(requests) == 1, thinking=True),
            )

        def build(settings, *, tools):
            return ClaudeChatModel(
                httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                {"model": settings.model_name},
                tools=tools,
                token_provider=token,
                model_info=sonnet_info(),
            )

        monkeypatch.setattr(model_factory, "build_chat_model", build)
    async with asyncio.timeout(120):
        await ReactLikeWorkflow().run(rc)
    assert len(calls) == 1 and calls[0][:2] == (17, 25)
    assert completed[-1][0] == RunTransition.COMPLETE
    assert completed[-1][1]["final_output"].strip() == (
        "COSIR_TOOL_OK:42" if live else "hello world"
    )
    text_deltas = [
        e.delta for e in events if isinstance(e, AssistantTextDeltaEvent) and e.part == "text"
    ]
    assert ("COSIR_TOOL_OK:42" if live else "hello world") in "".join(text_deltas)
    assert any(isinstance(e, AssistantTextDeltaEvent) and e.part == "reasoning" for e in events)
    assert rc.model.client.is_closed

    # 模拟重新创建上下文管理器，从 SQLite 读回再编码，验证不是仅内存中碰巧可回传。
    restored = RuntimeMessageContextManager(store.task.id, agent, store.context, store.workspace)
    restored.begin_run(run)
    _, native = encode_messages(restored.load_message())
    tool_messages = [m for m in native if any(b["type"] == "tool_use" for b in m["content"])]
    assert len(tool_messages) == 1
    blocks = tool_messages[0]["content"]
    assert any(b["type"] == "thinking" and b.get("signature") for b in blocks)
    assert any(b["type"] == "tool_use" and b["id"] == calls[0][2] for b in blocks)
    assert any(
        b["type"] == "tool_result" and b["tool_use_id"] == calls[0][2]
        for m in native
        for b in m["content"]
    )
    if not live:
        assert len(requests) == 2
        assert requests[1]["messages"][1]["content"] == blocks
