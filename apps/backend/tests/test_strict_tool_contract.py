"""所有工具统一 strict，且模型输入仍能走到原有执行与等待边界。"""

import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, ValidationError
from pydantic import ValidationError as ModelValidationError

from app.config.configuration import get_tool_system
from app.core.tools.schemas.model_tool_schema import strict_parameters_schema
from app.core.tools.tool_handler.agent_team.agent_team_run import AgentTeamRunTool
from app.core.tools.tool_handler.mcp import MCPTool
from app.core.tools.tool_models.agent_team.agent_team_args import AgentTeamArgs
from app.core.tools.tool_models.mcp_args import MCPArgs
from app.core.tools.tool_models.read_file_args import ReadFileArgs
from app.core.tools.tool_models.terminal_session_args import TerminalWriteArgs
from app.core.tools.validation.arguments import validate_tool_arguments
from app.task_runtime.task_runtime_space import TaskRuntimeSpace


def _assert_strict_schema(schema):
    """测试全部导出子树，防止新增工具绕过统一 schema 出口。"""
    if isinstance(schema, dict):
        assert not {"oneOf", "patternProperties", "discriminator"}.intersection(schema)
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False
            assert set(schema.get("required", [])) == set(schema.get("properties", {}))
        for value in schema.values():
            _assert_strict_schema(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_strict_schema(value)


@pytest.mark.usefixtures("backend_env")
def test_registered_tools_are_strict_and_survive_task_json_snapshot():
    definitions = get_tool_system().registry.get_all_definitions()
    assert {"mcp", "agent_team", "terminal_write", "execute_terminal"} <= {
        tool.name for tool in definitions
    }
    for tool in definitions:
        function = tool.to_model_tool_definition()
        assert function["strict"] is True, tool.name
        assert json.loads(json.dumps(function)) == function
        Draft202012Validator.check_schema(function["parameters"])
        _assert_strict_schema(function["parameters"])
        assert tool.normalized().to_model_tool_definition() == function


def test_default_fields_are_required_in_model_request_without_changing_local_defaults():
    schema = strict_parameters_schema(ReadFileArgs)
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({"path": "file.py"})
    arguments = {"path": "file.py", "offset": 1, "limit": 500}
    Draft202012Validator(schema).validate(arguments)
    result = validate_tool_arguments(arguments, schema, ReadFileArgs)
    assert result.ok
    assert result.arguments == arguments
    assert ReadFileArgs(path="file.py").offset == 1


def test_mcp_json_string_is_decoded_once_at_tool_argument_boundary(monkeypatch):
    value = {"nested": {"arbitrary-key": [None, True, 3, "汉字"]}}
    raw = {
        "action": "call",
        "server_name": "server",
        "tool_name": "remote",
        "arguments": json.dumps(value),
    }
    schema = strict_parameters_schema(MCPArgs)
    Draft202012Validator(schema).validate(raw)
    result = validate_tool_arguments(raw, schema, MCPArgs)
    assert result.ok
    assert result.arguments["arguments"] == value
    received = []

    async def call(server, name, arguments):
        received.append((server.name, name, arguments))
        return "done"

    monkeypatch.setattr("app.core.tools.tool_handler.mcp.read_mcp_configuration", lambda: None)
    monkeypatch.setattr(
        "app.core.tools.tool_handler.mcp.enabled_servers",
        lambda _: [SimpleNamespace(name="server")],
    )
    monkeypatch.setattr("app.core.tools.tool_handler.mcp.call_server_tool", call)
    observation = MCPTool().execute(**result.arguments)
    assert observation.status == "success"
    assert received == [("server", "remote", value)]
    # 校验不得改写上下文中持久化的原始模型参数。
    assert raw["arguments"] == json.dumps(value)


@pytest.mark.parametrize(
    "arguments",
    [{"old": "shape"}, "[]", "null", '{"unfinished":', '{"n": NaN}', '{"n": Infinity}'],
)
def test_mcp_rejects_old_object_shape_and_non_object_json(arguments):
    result = validate_tool_arguments(
        {"action": "call", "server_name": "s", "tool_name": "t", "arguments": arguments},
        strict_parameters_schema(MCPArgs),
        MCPArgs,
    )
    assert not result.ok


def _team_input():
    return {
        "team_id": "team",
        "goal": "交付",
        "node_goals": [{"node_id": "develop", "goal": "实现功能", "summary": "开发"}],
    }


def test_team_list_converts_to_domain_map_and_approved_replay_uses_decision(monkeypatch):
    raw = _team_input()
    schema = strict_parameters_schema(AgentTeamArgs)
    Draft202012Validator(schema).validate(raw)
    result = validate_tool_arguments(raw, schema, AgentTeamArgs)
    assert result.ok
    tool = object.__new__(AgentTeamRunTool)
    received = []
    profile = object()
    context = SimpleNamespace(
        agent=profile,
        event_loop=SimpleNamespace(is_closed=lambda: False),
        user_decision=None,
    )

    def prepare(team_id, goal, node_goals, execution_context, parent_profile):
        received.append({key: value.model_dump() for key, value in node_goals.items()})
        return "waiting"

    monkeypatch.setattr(tool, "_prepare_pending_run", prepare)
    assert tool.execute(**result.arguments, execution_context=context) == "waiting"
    assert received == [{"develop": {"goal": "实现功能", "summary": "开发"}}]
    decision = SimpleNamespace(kind="agent_team_review")
    # 使用生产决定种类；批准后的输入来自已持久化决定，不从模型列表重建。
    from app.core.tools.tool_handler.agent_team.agent_team_run import EXECUTING_DECISION_KINDS

    decision.kind = next(iter(EXECUTING_DECISION_KINDS))
    context.user_decision = decision
    monkeypatch.setattr(tool, "_start_confirmed_run", lambda d, p, _loop: (d, p))
    assert tool.execute(**result.arguments, execution_context=context) == (decision, profile)
    assert len(received) == 1


@pytest.mark.parametrize("kind", ["old_map", "duplicate", "reserved"])
def test_team_rejects_ambiguous_node_identity(kind):
    raw = _team_input()
    if kind == "old_map":
        raw["node_goals"] = {"develop": {"goal": "实现", "summary": "开发"}}
    elif kind == "duplicate":
        raw["node_goals"] *= 2
    else:
        raw["node_goals"][0]["node_id"] = "END"
    with pytest.raises(ModelValidationError):
        AgentTeamArgs.model_validate(raw)


@pytest.mark.parametrize(
    "input_value", [{"type": "line", "text": "echo ok"}, {"type": "key", "key": "ctrl_c"}]
)
def test_terminal_strict_union_preserves_input_operations(input_value):
    schema = strict_parameters_schema(TerminalWriteArgs)
    _assert_strict_schema(schema)
    raw = {"session_id": "s", "operation_id": "op", "input": input_value, "wait_ms": 500}
    Draft202012Validator(schema).validate(raw)
    result = validate_tool_arguments(raw, schema, TerminalWriteArgs)
    assert result.ok
    assert result.arguments == raw


@pytest.mark.parametrize("strict", [None, False, True])
def test_task_schema_boundary_rejects_old_snapshot_without_rewriting_it(strict):
    space = object.__new__(TaskRuntimeSpace)
    function = {"name": "read_file", "parameters": strict_parameters_schema(ReadFileArgs)}
    if strict is not None:
        function["strict"] = strict
    original = json.loads(json.dumps(function))
    space.current_task = SimpleNamespace(tool_schemas=[function])
    if strict is True:
        assert space.task_tool_schemas == (original,)
        assert space.task_tool_schemas[0] is not function
    else:
        with pytest.raises(ValueError, match="strict"):
            _ = space.task_tool_schemas
    assert function == original
