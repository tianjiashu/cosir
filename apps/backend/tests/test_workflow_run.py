"""ReactLikeWorkflow.run 的异步集成单测：验证工厂接入与端到端事件产出。

通过 MemorySaver 替换真实 SQLite checkpointer，用 GenericFakeChatModel（无 Key 经工厂产出）
作为模型，提供一个最小 operations 替身，真实驱动 graph 执行并收集 workflow.run 产出的
RuntimeEvent。覆盖 run 主体逻辑、graph 编译、factory 接入与事件翻译分支。

额外覆盖改动B：注入一个 ``bind_tools`` 抛 ``NotImplementedError`` 的自定义模型，验证
``run()`` 的 try/except 降级路径——不抛异常、降级为不绑定工具的基础模型、正常完成一轮。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from tests.conftest_stub_log import install_log_crud_stub

install_log_crud_stub()

from app.config.settings import BackendSettings  # noqa: E402
from app.core.workflows.react.workflow import ReactLikeWorkflow  # noqa: E402
from app.models import TaskRecord  # noqa: E402
from app.models.runtime_event import RuntimeEvent  # noqa: E402


def _settings(**overrides) -> BackendSettings:
    defaults = dict(
        project_root=Path("/tmp/ca"),
        log_dir=Path("/tmp/ca/logs"),
        database_file=Path("/tmp/ca/app.sqlite3"),
        model_base_url="https://api.deepseek.com/v1",
        model_api_key_env="DEEPSEEK_API_KEY",
        model_name="deepseek-v4-flash",
    )
    defaults.update(overrides)
    return BackendSettings(**defaults)


class _FakeTurn:
    turn_id = "turn-run-1"


class _FakeOperations:
    """最小 operations 替身，仅提供 workflow.run 所需的非模型接口。"""

    def __init__(self, settings: BackendSettings) -> None:
        self.settings = settings
        self._current_turn_id = "turn-run-1"

    def get_current_turn(self) -> _FakeTurn:
        return _FakeTurn()

    def model_tools(self):
        return []

    def build_messages(self):
        return []

    def has_turn_status(self, turn_id, status) -> bool:
        return False

    def update_turn_status(self, turn_id, status) -> None:
        pass

    def update_turn_response(self, turn_id, text) -> None:
        pass

    def log_exception(self, name, extra=None) -> None:
        pass

    def run_tool_calls(self, *args, **kwargs):  # 仅在 tools 节点触发，本路径不走到
        raise AssertionError("run_tool_calls should not be called in this test path")


@pytest.fixture()
def memory_checkpointer(monkeypatch):
    """用内存 MemorySaver 替换 SQLite checkpointer，避免依赖存储引擎初始化。"""

    @asynccontextmanager
    async def _fake_build() -> AsyncIterator[MemorySaver]:
        yield MemorySaver()

    import app.core.workflows.react.workflow as wfmod  # noqa: WPS433

    monkeypatch.setattr(wfmod, "build_checkpointer", _fake_build)
    yield


def _task() -> TaskRecord:
    return TaskRecord(
        task_id="task-1",
        workspace_id="ws-1",
        agent_id="agent-1",
        input_text="demo",
        title="demo",
        last_message_preview="",
        latest_turn_id=None,
        status="running",
        created_at=__import__("datetime").datetime.now(),
        updated_at=__import__("datetime").datetime.now(),
    )


@pytest.mark.asyncio
async def test_run_end_to_end_emits_events_with_factory_model(monkeypatch, memory_checkpointer) -> None:
    """无 Key 时工厂产出 fake model；run 端到端执行应产出 STEP/MODEL/FINAL 类事件并正常结束。

    可能发现的缺陷：workflow 未正确接入工厂（model=None 时未取到模型导致 AttributeError）；
    run 主体在真实 graph 执行下崩溃；事件翻译分支遗漏。
    """

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = _settings()
    ops = _FakeOperations(settings)
    wf = ReactLikeWorkflow()

    events: list[RuntimeEvent] = []
    async for ev in wf.run(_task(), ops, model=None):
        events.append(ev)

    assert len(events) > 0
    types = {str(e.event_type) for e in events}
    # fake model 产出文本 → 应至少经历 step 启动 / 模型完成 / 最终回答 / 运行结束
    assert "step_started" in types
    assert "model_completed" in types
    assert "final_response" in types
    assert "run_finished" in types


@pytest.mark.asyncio
async def test_run_with_injected_model_skips_factory(monkeypatch, memory_checkpointer) -> None:
    """注入 model 时，run 不应因 settings.model_name 而误用工厂；端到端仍正常结束。"""

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = _settings(model_name="deepseek-v4-pro")  # 即便 settings 指向 pro
    ops = _FakeOperations(settings)
    wf = ReactLikeWorkflow()
    fake = GenericFakeChatModel(
        messages=iter([AIMessage(content="injected response")])
    )

    events: list[RuntimeEvent] = []
    async for ev in wf.run(_task(), ops, model=fake):
        events.append(ev)

    assert len(events) > 0
    types = {str(e.event_type) for e in events}
    assert "run_finished" in types


@pytest.mark.asyncio
async def test_run_yields_model_output_delta_events(monkeypatch, memory_checkpointer) -> None:
    """模型流式分片经 messages 流应被翻译为 MODEL_OUTPUT_DELTA 事件（覆盖 token 提取分支）。

    可能发现的缺陷：_extract_token_text 在 messages 流路径下提取失败，导致增量事件丢失。
    """

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = _settings()
    ops = _FakeOperations(settings)
    wf = ReactLikeWorkflow()

    deltas = []
    async for ev in wf.run(_task(), ops, model=None):
        if str(ev.event_type) == "model_output_delta":
            deltas.append(ev)

    # GenericFakeChatModel 通过 messages 流产出文本分片，应至少产生一个增量事件
    assert len(deltas) >= 1
    for d in deltas:
        assert d.payload.get("text")


# ===================== 改动B：bind_tools 不支持时的降级路径 =====================


class _NoBindToolsFakeModel(GenericFakeChatModel):
    """自定义 chat model：``bind_tools`` 抛 NotImplementedError，模拟不支持工具绑定的模型。

    其它行为继承 GenericFakeChatModel（能正常产出 AIMessage），用于验证 run() 在
    bind_tools 失败时的 try/except 降级分支。
    """

    def bind_tools(self, tools, **kwargs):  # noqa: ANN, D102
        raise NotImplementedError("this model does not support bind_tools")


class _FakeOperationsWithTools(_FakeOperations):
    """返回非空工具列表的 operations 替身，使 model_tools_to_langchain 产出非空 tool_schemas，
    从而真正触发 base_model.bind_tools(...) 调用（而非 ``if tool_schemas else base_model`` 短路）。"""

    def model_tools(self):
        from app.tools.schemas.tool_definition import ToolDefinition  # noqa: WPS433

        def _noop(*_a, **_k):  # noqa: ANN
            return None

        return [
            ToolDefinition(
                name="search",
                description="search web",
                permission="read",
                required_params=["q"],
                handler=_noop,
                parameters_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                },
            )
        ]


@pytest.mark.asyncio
async def test_run_degrades_when_model_bind_tools_not_implemented(
    monkeypatch, memory_checkpointer, caplog
) -> None:
    """注入 bind_tools 抛 NotImplementedError 的模型（且模型暴露非空工具），run() 不应抛该异常，
    应降级为不绑定工具的基础模型并正常完成一轮（产出 run_finished 事件）。

    可能发现的缺陷：run() 未对 bind_tools 的 NotImplementedError 做降级，导致整个 workflow 崩溃；
    或降级时未 logger.warning 提示无 Key / 不支持工具。
    """

    import logging

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = _settings()
    ops = _FakeOperationsWithTools(settings)
    wf = ReactLikeWorkflow()
    fake = _NoBindToolsFakeModel(messages=iter([AIMessage(content="degraded response")]))

    events: list[RuntimeEvent] = []
    # 关键断言：不应向上抛 NotImplementedError（降级路径生效）
    async for ev in wf.run(_task(), ops, model=fake):
        events.append(ev)

    types = {str(e.event_type) for e in events}
    assert "run_finished" in types
    # 降级分支应记录 warning 日志
    assert any(
        rec.levelno == logging.WARNING and "bind_tools" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_run_degrades_without_tools_no_bind_called(
    monkeypatch, memory_checkpointer
) -> None:
    """当 operations 暴露空工具列表时，run() 走 ``if tool_schemas else base_model`` 短路，
    不调用 bind_tools，也能正常完成一轮（验证降级前置条件与默认路径协同）。

    可能发现的缺陷：空工具时仍强制调用 bind_tools 引发异常；或空工具时 graph 无法正常结束。
    """

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = _settings()
    ops = _FakeOperations(settings)  # model_tools() 返回 []
    wf = ReactLikeWorkflow()
    fake = _NoBindToolsFakeModel(messages=iter([AIMessage(content="no tools")]))

    events: list[RuntimeEvent] = []
    async for ev in wf.run(_task(), ops, model=fake):
        events.append(ev)

    types = {str(e.event_type) for e in events}
    assert "run_finished" in types
