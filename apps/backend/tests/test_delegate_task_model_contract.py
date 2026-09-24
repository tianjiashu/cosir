"""delegate_task 模型可见契约的回归测试。

背景（2026-09-17 缺陷）：工具定义在「子 Agent 注册表注入之前」注册时，会把模型可见
描述与参数 schema 在注册期快照固化，导致合法的 ``child_agent_id`` 列表永远到不了
模型——模型只能凭角色语义猜测（general / explorer / architect ...），连续
``delegate_task child not found`` 触发 ``tool_error_limit_reached``，整轮 run 失败。

本文件锁死两条不变量：
1. Agent registry 必须先于 ToolSystem 装配，模型可见契约在注册时即完整；
2. ``child_agent_id`` 必须以 enum 收口到真实注册 id，且任何情况下都不得把模板占位符
   ``{ids}`` 或空 enum 暴露给模型。
"""

from importlib import import_module

import pytest

from app.config import configuration
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.tools.schemas.tool_definition import ToolDefinition
from app.core.tools.tool_models.child_task.delegate_task_args import DelegateTaskArgs

child_agent_create = import_module("app.core.tools.tool_handler.child_task.child_agent_create")


def _model_payload(definition: ToolDefinition) -> dict:
    """取出工具定义投影给模型的 name/description/parameters。"""

    return definition.to_model_tool_definition()


def _child_agent_id_property(payload: dict) -> dict:
    """取出 payload 中 child_agent_id 的 schema 片段。"""

    return payload["parameters"]["properties"]["child_agent_id"]


@pytest.fixture
def registered_before_tool_system(monkeypatch) -> ToolDefinition:
    """验证 delegate_task 在 Agent registry 已就绪时固化完整模型契约。"""

    monkeypatch.setattr(configuration, "_TOOL_SYSTEM", None, raising=False)
    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)

    configuration.set_agent_registry(configuration.build_agent_registry())
    # 这里只验证 delegate_task 的定义快照；handler 的运行期 service 依赖不属于本契约，
    # 用 stub 隔离，避免测试要求先初始化 SQLite。
    monkeypatch.setattr(child_agent_create, "get_task_service", lambda: object())
    monkeypatch.setattr(child_agent_create, "get_conversation_run_service", lambda: object())
    monkeypatch.setattr(child_agent_create, "get_conversation_run_state_service", lambda: object())
    monkeypatch.setattr(child_agent_create, "get_conversation_run_executor", lambda: object())

    definition = child_agent_create.build_delegate_task_definition()
    assert definition is not None
    return definition


def test_delegate_task_description_exposes_child_agent_catalog(
    registered_before_tool_system: ToolDefinition,
) -> None:
    """模型必须能看到全部可委派子 Agent 的 id 与职责摘要。"""

    definition = registered_before_tool_system
    description = _model_payload(definition)["description"]

    assert "Available child agents" in description
    for agent_id in configuration.get_agent_registry().child_agent_ids():
        assert agent_id in description


def test_delegate_task_schema_restricts_child_agent_id_to_registered_ids(
    registered_before_tool_system: ToolDefinition,
) -> None:
    """child_agent_id 必须以 enum 收口到真实注册 id，模型无法猜错。"""

    definition = registered_before_tool_system
    child_property = _child_agent_id_property(_model_payload(definition))

    assert set(child_property["enum"]) == configuration.get_agent_registry().child_agent_ids()


def test_delegate_task_uses_static_model_snapshot(
    registered_before_tool_system: ToolDefinition,
) -> None:
    """描述与 schema 在注册时生成，delegate_task 不依赖运行期 provider。"""

    definition = registered_before_tool_system

    assert definition.parameters_schema

    snapshot = _model_payload(definition)
    configuration.set_agent_registry(AgentProfileRegistry())
    assert _model_payload(definition) == snapshot


def test_child_agent_id_schema_drops_placeholder_when_catalog_missing(monkeypatch) -> None:
    """注册表未就绪时，不得把模板占位符或空 enum 暴露给模型。"""

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)

    child_property = DelegateTaskArgs.model_json_schema()["properties"]["child_agent_id"]

    assert "{ids}" not in child_property["description"]
    assert "enum" not in child_property
