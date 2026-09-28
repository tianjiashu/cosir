"""delegate_task 的工具定义与目录授权输入契约测试。

工具定义不再按 workspace 做 Run 级投影（既无候选 ``enum`` 也不列举 ID 清单）：子 Agent 目录由
系统提示词的工具能力目录层下发，目标合法性由 handler 执行期解析 Registry 裁决。
"""

import json
import logging
from importlib import import_module
from pathlib import Path

import pytest

from app.config import configuration
from app.core.tools.schemas.tool_definition import ToolDefinition
from app.core.tools.schemas.tool_execution_context import ToolExecutionContext
from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.core.tools.tool_models.child_task.delegate_task_args import DelegateTaskArgs

child_agent_create = import_module("app.core.tools.tool_handler.child_task.child_agent_create")
child_agent_wait = import_module("app.core.tools.tool_handler.child_task.child_agent_wait")


def _model_payload(definition: ToolDefinition) -> dict:
    """取出工具定义投影给模型的 name/description/parameters。"""

    return definition.to_model_tool_definition()


def _child_agent_id_property(payload: dict) -> dict:
    """取出 child_agent_id 的参数 schema。"""

    return payload["parameters"]["properties"]["child_agent_id"]


@pytest.fixture
def base_delegate_definition(monkeypatch) -> ToolDefinition:
    """构造不含进程级候选的 delegate_task 基础定义。"""

    monkeypatch.setattr(child_agent_create, "get_task_service", lambda: object())
    monkeypatch.setattr(child_agent_create, "get_conversation_run_service", lambda: object())
    monkeypatch.setattr(child_agent_create, "get_conversation_run_state_service", lambda: object())
    monkeypatch.setattr(child_agent_create, "get_conversation_run_executor", lambda: object())
    definition = child_agent_create.build_delegate_task_definition()
    assert definition is not None
    return definition


def _write_workspace_agent(workspace: Path, agent_id: str) -> None:
    """写入测试用 workspace 子 Agent 配置。"""

    directory = workspace / ".cosir" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{agent_id}.json").write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "role": agent_id,
                "description": f"Workspace agent {agent_id}",
                "system_prompt": f"Prompt for {agent_id}",
                "allowed_tools": ["read_file"],
            }
        ),
        encoding="utf-8",
    )


def test_base_delegate_definition_does_not_freeze_candidates(
    base_delegate_definition: ToolDefinition,
) -> None:
    """工具定义只保留结构 schema：不固化候选、不列举 ID、描述不含子 Agent 目录。"""

    payload = _model_payload(base_delegate_definition)
    child_property = _child_agent_id_property(payload)

    assert "Available child agents" not in payload["description"]
    assert "enum" not in child_property
    assert "{ids}" not in child_property["description"]
    # 同回复内并行委派仍是工具调用契约：delegate_task 声明工具级并行，等待多个 child 的
    # 「同一回复里批量调用」提示由 child_agent_wait 承载（描述从 delegate_task 迁到了那里）。
    assert base_delegate_definition.parallel_mode == "parallel"
    assert "one reply" in child_agent_wait.ChildAgentWaitTool.description


def test_workspace_profiles_do_not_cross_workspace_boundaries(
    tmp_path: Path,
) -> None:
    """不同 workspace 的配置型子 Agent 不会进入彼此有效目录。"""

    system_registry = configuration.build_agent_registry()
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    _write_workspace_agent(workspace_a, "workspace-a-agent")
    _write_workspace_agent(workspace_b, "workspace-b-agent")

    system_registry.load_agent_profiles(workspace_a, workspace_a / ".cosir" / "agents")
    system_registry.load_agent_profiles(workspace_b, workspace_b / ".cosir" / "agents")

    assert system_registry.resolve(workspace_a, "workspace-a-agent") is not None
    assert system_registry.resolve(workspace_a, "workspace-b-agent") is None
    assert system_registry.resolve(workspace_b, "workspace-b-agent") is not None
    assert system_registry.resolve(workspace_b, "workspace-a-agent") is None


def test_delegate_args_only_validate_structure_and_budget() -> None:
    """候选 ID 的授权不隐式读取全局 registry，留给 handler 的 Run 快照校验。"""

    args = DelegateTaskArgs(
        child_agent_id="not-in-global-registry",
        agent_name="Scope",
        message="Perform the requested scoped task.",
    )

    assert args.child_agent_id == "not-in-global-registry"
    assert "enum" not in _child_agent_id_property(
        {"parameters": DelegateTaskArgs.model_json_schema()}
    )


@pytest.mark.parametrize("child_agent_id", ["other-workspace-only-agent", "main_agent"])
def test_delegate_handler_rejects_unavailable_or_non_child_profile_before_task_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    child_agent_id: str,
) -> None:
    """手工提交目录外 ID 或主 Agent ID 时，在创建 child Task 前拒绝。"""

    class _TaskSpy:
        def __init__(self) -> None:
            self.created = False

        def get_or_create_task(self, **_kwargs) -> None:
            self.created = True
            raise AssertionError("不应为无效 agent_id 创建 child Task")

    task_spy = _TaskSpy()
    monkeypatch.setattr(child_agent_create, "get_task_service", lambda: task_spy)
    monkeypatch.setattr(child_agent_create, "get_conversation_run_service", lambda: object())
    monkeypatch.setattr(child_agent_create, "get_conversation_run_state_service", lambda: object())
    monkeypatch.setattr(child_agent_create, "get_conversation_run_executor", lambda: object())
    tool = child_agent_create.DelegateTaskTool()
    runtime_dependencies = ToolRuntimeDependencies(
        parent_agent_profile=configuration.build_agent_registry().resolve("system", "main_agent"),
        agent_profile_registry=configuration.build_agent_registry(),
    )
    context = ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=2,
        runtime_dependencies=runtime_dependencies,
    )

    result = tool.execute(
        child_agent_id=child_agent_id,
        agent_name="Scope",
        message="Perform this scoped task.",
        execution_context=context,
    )

    assert result.status == "error"
    assert task_spy.created is False


def test_workspace_config_error_is_not_silently_merged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """无效 workspace 配置不得被静默合并：坏文件只被跳过并留 error 日志，不注册任何 profile。

    目录级问题（目录不是目录 / 符号链接越界等）才抛 ``AgentProfileConfigError``（``ValueError``
    子类），由启动编排捕获后禁用该 workspace 的委派。
    """

    workspace = tmp_path / "workspace"
    directory = workspace / ".cosir" / "agents"
    directory.mkdir(parents=True)
    broken = directory / "broken.json"
    broken.write_text("{", encoding="utf-8")

    registry = configuration.build_agent_registry()
    with caplog.at_level(logging.ERROR):
        registry.load_agent_profiles(workspace, directory)

    # 坏配置未进入 registry，也没有从 system 作用域回退出一个同名 profile。
    assert registry.resolve(workspace, "broken") is None
    records = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert any(
        record.message == "agent_profile_config_invalid" for record in records
    ), "坏配置必须写 error 日志留痕，否则「跳过」无法排查"

    # 目录级问题才向上抛出，调用方据此禁用该 workspace 的委派工具。
    not_a_directory = workspace / ".cosir" / "agents-file"
    not_a_directory.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="Agent 配置路径不是目录"):
        configuration.build_agent_registry().load_agent_profiles(
            workspace,
            not_a_directory,
        )
