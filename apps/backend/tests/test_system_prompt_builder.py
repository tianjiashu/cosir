"""系统提示词构建测试。"""

from collections.abc import AsyncIterator
from datetime import date, datetime
from pathlib import Path
from platform import system
from typing import Any, cast

from app.core.agents.agent_profile import DEFAULT_DEVELOPER_TOOLS, AgentProfile
from app.core.context import RuntimeContextBuilder, SystemPromptBuilder, SystemPromptContext
from app.core.llm.model_settings import ModelSettings
from app.models import RuntimeMessage, TurnRecord
from app.tools.schemas import ToolExecutionContext


class FakeWorkflow:
    """隔离测试用工作流，避免初始化完整 LangGraph runtime。"""

    workflow_id = "fake"

    async def run(self, task: Any, operations: object) -> AsyncIterator[object]:
        """提供满足 AgentProfile 协议的空运行实现。

        参数:
            task: 测试中不会消费的任务记录。
            operations: 测试中不会消费的运行时操作门面。

        生成:
            无。

        异常:
            无。

        副作用:
            无。
        """

        if task is operations:
            yield task


class MemoryMessageStore:
    """提供测试用历史消息读取能力。"""

    def __init__(self, messages_by_turn: dict[str, list[RuntimeMessage]]) -> None:
        """初始化内存消息存储。

        参数:
            messages_by_turn: 按 turn_id 组织的运行时消息列表。

        返回:
            无。

        异常:
            无。

        副作用:
            保存传入的消息映射引用。
        """

        self._messages_by_turn = messages_by_turn

    def load_turn_messages(self, turn_id: str) -> list[RuntimeMessage]:
        """读取指定轮次的历史消息。

        参数:
            turn_id: 需要读取的轮次标识。

        返回:
            该轮次对应的运行时消息列表；缺失时返回空列表。

        异常:
            无。

        副作用:
            无。
        """

        return self._messages_by_turn.get(turn_id, [])


def _turn(turn_id: str, task_id: str = "task-1", input_text: str = "实现功能") -> TurnRecord:
    """构造测试用轮次记录。

    参数:
        turn_id: 轮次标识。
        task_id: 任务标识。
        input_text: 用户输入文本。

    返回:
        可交给上下文构建器消费的 ``TurnRecord``。

    异常:
        无。

    副作用:
        无。
    """

    now = datetime(2026, 7, 30)
    return TurnRecord(
        turn_id=turn_id,
        task_id=task_id,
        input_text=input_text,
        status="pending",
        created_at=now,
        updated_at=now,
    )


def _agent(
    agent_id: str = "developer",
    role: str = "developer",
    allowed_tools: list[str] | None = None,
    model_name: str = "deepseek-v4-flash",
    model_settings: ModelSettings | None = None,
) -> AgentProfile:
    """构造测试用 AgentProfile。

    参数:
        agent_id: Agent 稳定标识。
        role: Agent 角色。
        allowed_tools: 允许工具列表；为空时使用默认开发工具。
        model_name: 模型名称。
        model_settings: 模型设置；为空时使用默认设置。

    返回:
        注入 fake workflow 的 ``AgentProfile``。

    异常:
        无。

    副作用:
        无。
    """

    return AgentProfile(
        agent_id=agent_id,
        role=role,
        allowed_tools=list(DEFAULT_DEVELOPER_TOOLS) if allowed_tools is None else allowed_tools,
        context_policy="text_only_v1",
        workflow=cast(Any, FakeWorkflow()),
        model_name=model_name,
        model_settings=model_settings
        or ModelSettings(
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        ),
    )


def test_system_prompt_builder_keeps_section_order() -> None:
    """验证系统提示词 section 顺序稳定。

    参数:
        无。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        无。
    """

    agent = _agent()
    context = SystemPromptContext.build(agent, _turn("turn-1"), "H:\\coding-agent")

    prompt = SystemPromptBuilder().build(agent, context)

    expected_order = [
        "<agent_identity>",
        "<mission>",
        "<engineering_principles>",
        "<workflow_contract>",
        "<context_policy>",
        "<tool_use_policy>",
        "<provider_overlay>",
    ]
    positions = [prompt.index(section) for section in expected_order]
    assert positions == sorted(positions)


def test_system_prompt_builder_injects_agent_profile_fields() -> None:
    """验证 Agent 档案字段被注入系统提示词。

    参数:
        无。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        无。
    """

    agent = _agent()
    context = SystemPromptContext.build(agent, _turn("turn-1"), "H:\\coding-agent")

    prompt = SystemPromptBuilder().build(agent, context)

    assert "Agent ID: developer" in prompt
    assert "Role: developer" in prompt
    assert f"OS: {system() or 'unknown'}" in prompt
    assert "Workspace root: H:\\coding-agent" in prompt
    assert f"Today: {date.today().isoformat()}" in prompt
    assert "Policy: text_only_v1" in prompt
    assert f"Allowed tools: {', '.join(DEFAULT_DEVELOPER_TOOLS)}" in prompt
    assert "Task ID:" not in prompt
    assert "Turn ID:" not in prompt
    assert "Model:" not in prompt


def test_system_prompt_builder_renders_none_for_empty_tools() -> None:
    """验证空工具列表渲染为 none。

    参数:
        无。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        无。
    """

    agent = _agent(
        agent_id="readonly",
        role="reviewer",
        allowed_tools=[],
        model_name="gpt-4o",
        model_settings=ModelSettings(),
    )
    context = SystemPromptContext.build(agent, _turn("turn-1"))

    prompt = SystemPromptBuilder().build(agent, context)

    assert "Allowed tools: none" in prompt


def test_system_prompt_builder_only_adds_deepseek_overlay_for_deepseek() -> None:
    """验证 DeepSeek overlay 只在 DeepSeek 模型下出现。

    参数:
        无。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        无。
    """

    deepseek_agent = _agent()
    other_agent = _agent(
        agent_id="other",
        role="developer",
        allowed_tools=["read_file"],
        model_name="gpt-4o",
        model_settings=ModelSettings(),
    )

    deepseek_prompt = SystemPromptBuilder().build(
        deepseek_agent, SystemPromptContext.build(deepseek_agent, _turn("turn-1"))
    )
    other_prompt = SystemPromptBuilder().build(
        other_agent, SystemPromptContext.build(other_agent, _turn("turn-1"))
    )

    assert "<provider_overlay>" in deepseek_prompt
    assert "DeepSeek:" in deepseek_prompt
    assert "<provider_overlay>" not in other_prompt


def test_text_context_builder_keeps_message_order() -> None:
    """验证文本上下文消息顺序仍为 system、历史消息、当前用户输入。

    参数:
        无。

    返回:
        无。

    异常:
        断言失败时由 pytest 抛出。

    副作用:
        无。
    """

    prior_turn = _turn("turn-1", input_text="上一轮")
    current_turn = _turn("turn-2", input_text="当前轮")
    store = MemoryMessageStore(
        {
            "turn-1": [
                RuntimeMessage(role="user", content_text="上一轮"),
                RuntimeMessage(role="assistant", content_text="上一轮回复"),
            ]
        }
    )
    execution_context = ToolExecutionContext(
        task_id="task-1",
        workspace_id="workspace-1",
        workspace_root=Path("H:/coding-agent"),
    )

    messages = RuntimeContextBuilder().build_messages(
        _agent(),
        current_turn,
        [prior_turn, current_turn],
        store,
        execution_context=execution_context,
    )

    assert [message.role for message in messages] == ["system", "user", "assistant", "user"]
    assert messages[0].content_text.startswith("<agent_identity>")
    assert "Workspace root: H:\\coding-agent" in messages[0].content_text
    assert messages[-1].content_text == "当前轮"
