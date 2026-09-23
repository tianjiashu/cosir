"""delegate_task 模型可见契约的回归测试。

背景（2026-09-17 缺陷）：工具定义在「子 Agent 注册表注入之前」注册时，会把模型可见
描述与参数 schema 在注册期快照固化，导致合法的 ``child_agent_id`` 列表永远到不了
模型——模型只能凭角色语义猜测（general / explorer / architect ...），连续
``delegate_task child not found`` 触发 ``tool_error_limit_reached``，整轮 run 失败。

本文件锁死两条不变量：
1. 注册时机不得影响模型可见契约（注册在注册表注入之前也必须正确）；
2. ``child_agent_id`` 必须以 enum 收口到真实注册 id，且任何情况下都不得把模板占位符
   ``{ids}`` 或空 enum 暴露给模型。
"""

import pytest

from app.config import configuration
from app.core.tools.schemas.tool_definition import ToolDefinition
from app.core.tools.tool_models.child_task.delegate_task_args import DelegateTaskArgs
from app.core.tools.tool_system import ToolSystem


def _model_payload(definition: ToolDefinition) -> dict:
    """取出工具定义投影给模型的 name/description/parameters。"""

    return definition.to_model_tool_definition()


def _child_agent_id_property(payload: dict) -> dict:
    """取出 payload 中 child_agent_id 的 schema 片段。"""

    return payload["parameters"]["properties"]["child_agent_id"]


@pytest.fixture
def registered_before_catalog(monkeypatch) -> tuple[ToolSystem, ToolDefinition]:
    """复刻 app.py 启动顺序装配单例，并返回「注册期」拿到的 delegate_task 定义。

    装配顺序与 ``app/app.py`` 一致：先 ``ToolSystem.build_tool_system``（此时 agent
    注册表尚未注入），再 ``set_agent_registry(build_agent_registry())``。测试结束后
    monkeypatch 自动还原进程级单例。
    """

    monkeypatch.setattr(configuration, "_TOOL_SYSTEM", None, raising=False)
    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)

    tool_system = ToolSystem.build_tool_system()
    configuration.set_tool_system(tool_system)

    definition = tool_system.registry.get_tool_definition("delegate_task")
    assert definition is not None

    # 确认注册确实发生在子 Agent 注册表注入之前
    with pytest.raises(RuntimeError):
        configuration.get_agent_registry()
    configuration.set_agent_registry(configuration.build_agent_registry())
    return tool_system, definition


def test_delegate_task_description_exposes_child_agent_catalog(
    registered_before_catalog: tuple[ToolSystem, ToolDefinition],
) -> None:
    """模型必须能看到全部可委派子 Agent 的 id 与职责摘要。"""

    _, definition = registered_before_catalog
    description = _model_payload(definition)["description"]

    assert "Available child agents" in description
    for agent_id in configuration.get_agent_registry().child_agent_ids():
        assert agent_id in description


def test_delegate_task_schema_restricts_child_agent_id_to_registered_ids(
    registered_before_catalog: tuple[ToolSystem, ToolDefinition],
) -> None:
    """child_agent_id 必须以 enum 收口到真实注册 id，模型无法猜错。"""

    _, definition = registered_before_catalog
    child_property = _child_agent_id_property(_model_payload(definition))

    assert set(child_property["enum"]) == configuration.get_agent_registry().child_agent_ids()


def test_child_agent_id_schema_drops_placeholder_when_catalog_missing(monkeypatch) -> None:
    """注册表未就绪时，不得把模板占位符或空 enum 暴露给模型。"""

    monkeypatch.setattr(configuration, "_AGENT_REGISTRY", None, raising=False)

    child_property = DelegateTaskArgs.model_json_schema()["properties"]["child_agent_id"]

    assert "{ids}" not in child_property["description"]
    assert "enum" not in child_property
