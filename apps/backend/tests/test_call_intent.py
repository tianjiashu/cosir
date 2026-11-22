"""调用意图契约：只由参数难读的工具自己声明，是普通业务参数；实时与冷重建都按原样参数投影。"""

import json
from types import SimpleNamespace

import pytest
from app.assistant_transport.projection.snapshot_rebuild import pair_tool_parts
from app.assistant_transport.projection.tool_call_event_projection import plan_tool_call_arguments
from app.config.configuration import get_tool_registry
from app.core.events import ToolCallArgumentsEvent, ToolCallStatusChangedEvent
from app.core.tool.execute.tool_executor import ToolExecutor
from app.core.tool.schemas import (
    TOOL_BROWSER_EVALUATE,
    TOOL_EXECUTE_TERMINAL,
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.request_tools import RequestTools
from app.core.tool.tool_registry import ToolRegistry
from app.core.workflow.node_helper import model_stream_state_machine as stream
from app.tools.tool_models.call_intent import INTENT_FIELD, intent_field
from pydantic import BaseModel, ConfigDict

_INTENT_TOOLS = {TOOL_EXECUTE_TERMINAL, TOOL_BROWSER_EVALUATE}


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent: str = intent_field()
    value: int


def context(tmp_path):
    return ToolExecutionContext(
        session_id=1, workspace_id=1, run_id=2, step_id="s", workspace_root=tmp_path, tool_call_id="c"
    )


@pytest.mark.usefixtures("backend_env")
def test_only_tools_with_hard_to_read_arguments_declare_intent():
    registry = get_tool_registry()
    declaring = set()
    for name in registry.get_all_tool_names():
        schema = registry.get_tool_definition(name).to_model_tool_definition()["parameters"]
        if INTENT_FIELD in schema["properties"]:
            declaring.add(name)
            # 对模型是必填字符串（strict 全量 required），不是可空字段。
            assert schema["properties"][INTENT_FIELD]["type"] == "string", name
            assert INTENT_FIELD in schema["required"], name
    assert declaring == _INTENT_TOOLS


@pytest.mark.parametrize("arguments", [{"value": 1}, {"intent": "", "value": 1}])
def test_missing_intent_still_executes(tmp_path, monkeypatch, arguments):
    """意图只服务展示：漏写不拒绝调用，handler 收到空串。"""
    seen = []

    def handler(intent, value, *, execution_context):
        seen.append((intent, value))
        return ToolObservation(tool_name="probe", status="success", content="ok")

    registry = ToolRegistry(
        [ToolDefinition(name="probe", group="test", description="probe", args_model=Args, handler=handler)]
    )
    monkeypatch.setattr("app.core.tool.execute.tool_executor.emit", lambda event: None)
    call = ToolCall(tool_name="probe", call_id="c", arguments=json.dumps(arguments))
    result = ToolExecutor(registry).execute(
        call, context(tmp_path), RequestTools(schemas=(), admitted=frozenset({"probe"}))
    )
    assert result.status == "success"
    assert seen == [("", 1)]


def test_tools_without_the_field_reject_an_intent_argument(tmp_path, monkeypatch):
    """不声明意图的工具不认这个名字：它不是平台字段，写了就是多余参数。"""

    class Plain(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        value: int

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="probe", group="test", description="probe", args_model=Plain,
                handler=lambda value, *, execution_context: pytest.fail("must not run"),
            )
        ]
    )
    monkeypatch.setattr("app.core.tool.execute.tool_executor.emit", lambda event: None)
    call = ToolCall(tool_name="probe", call_id="c", arguments='{"intent":"x","value":1}')
    result = ToolExecutor(registry).execute(
        call, context(tmp_path), RequestTools(schemas=(), admitted=frozenset({"probe"}))
    )
    assert result.status == "error"


@pytest.mark.parametrize("raw", ['{"intent":"检查配置","command":"ls"}', '{"intent":"检查配置",'])
@pytest.mark.parametrize("ending", ["finish", "cancel"])
def test_stream_close_arguments_match_cold_projection_even_without_execution(monkeypatch, raw, ending):
    events = []
    monkeypatch.setattr(stream, "emit", events.append)
    monkeypatch.setattr(
        stream, "_message_context", lambda: SimpleNamespace(add_message=lambda *a, **kw: None)
    )
    machine = stream.ModelStreamStateMachine(1, 2, "s")
    call = SimpleNamespace(id="c", function=SimpleNamespace(name="execute_terminal", arguments=raw))
    machine.tool_call([call])
    getattr(machine, ending)()
    machine.finish()
    argument_events = [e for e in events if isinstance(e, ToolCallArgumentsEvent)]
    assert len(argument_events) == 1
    assert not any(isinstance(e, ToolCallStatusChangedEvent) and e.status == "running" for e in events)
    rows = [
        SimpleNamespace(
            message={
                "role": "assistant",
                "tool_calls": [
                    {"id": "c", "type": "function", "function": {"name": "execute_terminal", "arguments": raw}}
                ],
            },
            metadata=None,
            sequence=1,
        )
    ]
    parts = pair_tool_parts(rows, {})
    # 意图留在参数里，part 没有单独的意图字段；前端工具目录从 ``args.intent`` 取标题。
    assert "intent" not in parts["c"]
    assert parts["c"]["args"] == argument_events[0].args
    state = {"messages": [{"parts": [parts["c"]]}]}
    [mutation] = plan_tool_call_arguments(argument_events[0], state)
    assert mutation.path[-1] == "args"
    assert mutation.value == parts["c"]["args"]
