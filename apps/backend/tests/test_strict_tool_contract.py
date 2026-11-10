"""所有工具统一 strict，且模型输入仍能走到原有执行与等待边界。"""

import asyncio
import json
from pathlib import Path
import re
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, ValidationError
from mcp import types
from pydantic import ValidationError as ModelValidationError

from app.config.configuration import get_tool_registry
from app.core.tool.multi_agent.agent_team_run import AgentTeamRunTool
from app.core.tool.multi_agent.args.agent_team_args import AgentTeamArgs
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.tool.validation.arguments import validate_tool_arguments
from app.task_runtime.task_runtime_space import TaskRuntimeSpace
from app.tools.tool_handler.mcp_tool import MCPTool
from app.core.tool.schemas import ToolExecutionContext
from app.tools.tool_models.mcp_args import MCPArgs
from app.tools.tool_models.read_file_args import ReadFileArgs
from app.tools.tool_models.terminal_session_args import TerminalWriteArgs


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


_CJK = re.compile(r"[\u4e00-\u9fff]")


def _iter_property_groups(schema) -> list[dict[str, dict]]:
    """递归收集每个 ``properties`` 层级。

    按层级返回而非摊平：同一 union 的不同分支里同名字段共享语义，描述本就应当一致，
    只有同一层级内两个不同字段共用一段描述才是复制粘贴。
    """

    groups: list[dict[str, dict]] = []
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            groups.append(properties)
        for value in schema.values():
            groups.extend(_iter_property_groups(value))
    elif isinstance(schema, list):
        for value in schema:
            groups.extend(_iter_property_groups(value))
    return groups


def _is_discriminator(field_schema: dict) -> bool:
    """字段取值是否由自身表达（``const`` 或单值 ``enum``）。"""

    if "const" in field_schema:
        return True
    values = field_schema.get("enum")
    return isinstance(values, list) and len(values) == 1


@pytest.mark.usefixtures("backend_env")
def test_registered_tool_prompts_are_english_and_documented():
    """全部工具的模型可见文案：英文、非空、每个字段都有说明且互不重复。

    模型看不到实现，只看到这些文本；缺一条就少一个决策依据，多一份重复就多一处漂移。
    """

    problems: list[str] = []
    for tool in get_tool_registry().get_all_definitions():
        function = tool.to_model_tool_definition()
        description = function["description"]
        if not description or description != description.strip():
            problems.append(f"{tool.name}: 工具描述为空或含首尾空白")
        if _CJK.search(description):
            problems.append(f"{tool.name}: 工具描述含非英文文本")
        if _CJK.search(json.dumps(function["parameters"], ensure_ascii=False)):
            problems.append(f"{tool.name}: 参数 schema 含非英文文本")

        for group in _iter_property_groups(function["parameters"]):
            descriptions = []
            for field_name, field_schema in group.items():
                # 判别字段（union 的 type）：取值由 const / 单值 enum 自身表达，不另写描述。
                if _is_discriminator(field_schema):
                    continue
                text = field_schema.get("description")
                if not isinstance(text, str) or not text.strip():
                    problems.append(f"{tool.name}.{field_name}: 字段缺少描述")
                    continue
                if _CJK.search(text):
                    problems.append(f"{tool.name}.{field_name}: 字段描述含非英文文本")
                descriptions.append(text)
            duplicated = {text for text in descriptions if descriptions.count(text) > 1}
            if duplicated:
                problems.append(f"{tool.name}: 同层字段描述重复 {sorted(duplicated)}")

    assert not problems, "\n".join(problems)


@pytest.mark.usefixtures("backend_env")
def test_registered_tools_are_strict_and_survive_task_json_snapshot():
    definitions = get_tool_registry().get_all_definitions()
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

    class _Pool:
        async def call_tool(self, server, name, arguments):
            received.append((server.name, name, arguments))
            return types.CallToolResult(content=[types.TextContent(type="text", text="done")])

        def submit(self, coroutine):
            future = Future()
            future.set_result(asyncio.run(coroutine))
            return future

    monkeypatch.setattr(
        "app.tools.tool_handler.mcp_tool.available_servers",
        lambda _: [SimpleNamespace(name="server", scope=SimpleNamespace(kind="system"))],
    )
    monkeypatch.setattr("app.service.depends.get_mcp_connection_pool", _Pool)
    context = ToolExecutionContext(
        task_id=1, workspace_id=1, workspace_root=Path("."), step_id="step", run_id=1
    )
    observation = MCPTool().execute(**result.arguments, execution_context=context)
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


def test_team_list_converts_to_domain_map(monkeypatch):
    raw = _team_input()
    schema = strict_parameters_schema(AgentTeamArgs)
    Draft202012Validator(schema).validate(raw)
    result = validate_tool_arguments(raw, schema, AgentTeamArgs)
    assert result.ok
    tool = object.__new__(AgentTeamRunTool)
    received = []
    context = SimpleNamespace(agent=object(), user_decision=None)

    def prepare(team_id, goal, node_goals, execution_context, parent_profile):
        received.append({key: value.model_dump() for key, value in node_goals.items()})
        return "awaiting_review"

    monkeypatch.setattr(tool, "_prepare_pending_run", prepare)
    assert tool.execute(**result.arguments, execution_context=context) == "awaiting_review"
    assert received == [{"develop": {"goal": "实现功能", "summary": "开发"}}]


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
    "command",
    ["echo ok", "cd /tmp && ls", "git status"],
)
def test_terminal_write_prefills_one_command_line(command):
    schema = strict_parameters_schema(TerminalWriteArgs)
    _assert_strict_schema(schema)
    raw = {"session_id": "s", "command": command}
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
