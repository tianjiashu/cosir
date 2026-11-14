"""统一意图契约：模型出口、执行边界及独立实时/冷重建投影。"""

import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from app.assistant_transport.projection.snapshot_rebuild import (
    pair_tool_parts,
)
from app.assistant_transport.projection.tool_call_event_projection import plan_tool_call_intent
from app.config.configuration import get_tool_registry
from app.core.events import ToolCallIntentEvent, ToolCallStatusChangedEvent
from app.core.tool.call_intent import display_call_arguments, model_parameters_with_intent
from app.core.tool.execute.tool_access_gate import ToolAccessGate
from app.core.tool.execute.tool_executor import ToolExecutor
from app.core.tool.schemas import ToolCall, ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tool.tool_registry import ToolRegistry
from app.core.workflow.node_helper import model_stream_state_machine as stream
from app.tools.tool_models.mcp_args import MCPArgs


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: int


def context(tmp_path):
    return ToolExecutionContext(
        session_id=1, workspace_id=1, run_id=2, step_id="s", workspace_root=tmp_path, tool_call_id="c"
    )


@pytest.mark.usefixtures("backend_env")
def test_all_registered_model_schemas_inject_once_without_changing_business_schema():
    registry = get_tool_registry()
    for name in registry.get_all_tool_names():
        definition = registry.get_tool_definition(name)
        before = json.dumps(dict(definition.parameters_schema), sort_keys=True)
        schema = definition.to_model_tool_definition()["parameters"]
        assert next(iter(schema["properties"])) == "intent", name
        assert schema["required"].count("intent") == 1
        assert "intent" not in definition.args_model.model_fields
        assert json.dumps(dict(definition.parameters_schema), sort_keys=True) == before
        schema["properties"].pop("intent")
        schema["required"].remove("intent")
        assert schema == definition.parameters_schema


def test_reserved_conflict_and_deep_copy():
    schema = {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/X"}},
        "required": ["x"],
        "$defs": {"X": {"type": "string"}},
        "additionalProperties": False,
    }
    result = model_parameters_with_intent(schema)
    result["$defs"]["X"]["type"] = "number"
    assert schema["$defs"]["X"]["type"] == "string"
    with pytest.raises(ValueError, match="reserved"):
        model_parameters_with_intent({"properties": {"intent": {}}})


@pytest.mark.parametrize("intent", [None, "", "  ", 5, "x" * 161])
def test_invalid_intent_never_reaches_handler_or_running(tmp_path, monkeypatch, intent):
    def handler(**kwargs):
        pytest.fail("invalid intent reached handler")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="probe", group="test", description="probe", args_model=Args, handler=handler
            )
        ]
    )
    events = []
    monkeypatch.setattr("app.core.tool.execute.tool_executor.emit", events.append)
    call = ToolCall(
        tool_name="probe", call_id="c", arguments=json.dumps({"intent": intent, "value": 1})
    )
    result = ToolExecutor(registry).execute(call, context(tmp_path), {"probe"})
    assert result.status == "error"
    assert not events


def test_handler_only_receives_business_args_context_keeps_original_intent(
    tmp_path, monkeypatch
):
    seen = []

    def handler(value, *, execution_context):
        seen.append(
            (
                value,
                execution_context.call_intent,
                execution_context.for_process_execution().call_intent,
            )
        )
        return ToolObservation(tool_name="probe", status="success", content="ok")

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="probe", group="test", description="probe", args_model=Args, handler=handler
            )
        ]
    )

    monkeypatch.setattr("app.core.tool.execute.tool_executor.emit", lambda event: None)
    call = ToolCall(
        tool_name="probe", call_id="c", arguments='{"intent":"  检查数据  ","value":"1"}'
    )
    assert ToolExecutor(registry).execute(call, context(tmp_path), {"probe"}).status == "success"
    # ``intent`` 是调用意图，不进业务参数：收紧空白后只留在运行上下文里。
    assert seen == [(1, "检查数据", "检查数据")]


def test_mcp_json_normalization_is_not_revalidated(tmp_path):
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="mcp",
                group="test",
                description="probe",
                args_model=MCPArgs,
                handler=lambda **kwargs: None,
            )
        ]
    )
    call = ToolCall(
        tool_name="mcp",
        call_id="c",
        arguments=json.dumps(
            {
                "intent": "查询服务",
                "action": "call",
                "server_name": "test",
                "tool_name": "lookup",
                "arguments": '{"query":"x"}',
            }
        ),
    )
    result = ToolAccessGate(registry).evaluate(call, context(tmp_path))
    assert result.denial is None
    assert result.arguments["arguments"] == {"query": "x"}
    assert "intent" not in result.arguments


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"intent":"检查配置","bad":true}', "检查配置"),
        ('{"intent":"检查配置",', None),
        ('{"intent":42}', None),
    ],
)
@pytest.mark.parametrize("ending", ["finish", "cancel"])
def test_stream_close_intent_matches_cold_projection_even_without_execution(
    monkeypatch, raw, expected, ending
):
    events = []
    monkeypatch.setattr(stream, "emit", events.append)
    monkeypatch.setattr(
        stream, "get_tool_registry", lambda: SimpleNamespace(get_tool_definition=lambda _: object())
    )
    monkeypatch.setattr(
        stream, "_message_context", lambda: SimpleNamespace(add_message=lambda *a, **kw: None)
    )
    machine = stream.ModelStreamStateMachine(1, 2, "s")
    call = SimpleNamespace(id="c", function=SimpleNamespace(name="read_file", arguments=raw))
    machine.tool_call([call])
    getattr(machine, ending)()
    machine.finish()
    intent_events = [e for e in events if isinstance(e, ToolCallIntentEvent)]
    assert len(intent_events) == 1
    assert intent_events[0].intent == expected
    assert not any(
        isinstance(e, ToolCallStatusChangedEvent) and e.status == "running" for e in events
    )
    assert display_call_arguments(raw)[0] == expected
    # Cold rebuild consumes the same persisted function.arguments, including invalid business args.
    rows = [
        SimpleNamespace(
            message={
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": raw},
                    }
                ],
            },
            metadata=None,
            sequence=1,
        )
    ]
    parts = pair_tool_parts(rows, {})
    assert parts["c"]["intent"] == expected
    assert "intent" not in parts["c"]["args"]
    state = {"messages": [{"parts": [parts["c"]]}]}
    mutation = plan_tool_call_intent(intent_events[0], state)[0]
    assert mutation.path[-1] == "intent"
    assert mutation.value == expected


def process_intent_probe(value, *, execution_context):
    """可 pickle 的真实子进程 handler；若意图作为业务 kwarg 泄漏，调用直接失败。"""
    return ToolObservation(
        tool_name="probe", status="success", content=f"{value}:{execution_context.call_intent}"
    )


@pytest.mark.parametrize("mode", ["thread", "process"])
def test_intent_reaches_actual_handler_context_in_both_execution_modes(tmp_path, monkeypatch, mode):
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="probe",
                group="test",
                description="probe",
                args_model=Args,
                handler=process_intent_probe,
                execution_mode=mode,
            )
        ]
    )
    monkeypatch.setattr("app.core.tool.execute.tool_executor.emit", lambda _: None)
    result = ToolExecutor(registry).execute(
        ToolCall(tool_name="probe", call_id="c", arguments='{"intent":"读取样例","value":3}'),
        context(tmp_path),
        {"probe"},
    )
    assert result.status == "success" and result.content == "3:读取样例"
