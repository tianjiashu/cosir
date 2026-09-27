"""配置中心工具组投影的方向一致性测试。

覆盖 API schemas 层的两个方向：``AgentConfigurationDocument.from_payload`` 把请求携带的工具组展开
为工具名（写入方向），``AgentConfigurationResponse.from_document`` 把工具名反向聚合为工具组（读取
方向）。两者都接真实 ``ToolRegistry``——换算规则由注册表实现，测试不复刻一遍规则，避免「替身与
生产实现漂移」。
"""

from __future__ import annotations

import importlib

import pytest
from pydantic import BaseModel

from app.api.schemas.AgentConfigurationDocument import AgentConfigurationDocument
from app.api.schemas.request.AgentConfigurationRequest import AgentConfigurationRequest
from app.api.schemas.response.AgentConfigurationResponse import AgentConfigurationResponse
from app.core.tools.schemas.tool_definition import ToolDefinition
from app.core.tools.tool_registry import ToolRegistry


class _PlaceholderArgs(BaseModel):
    """占位参数模型；本测试只消费工具的名称、分组与描述。"""


def _definition(name: str, group: str) -> ToolDefinition:
    """构造一个注册表可接受的工具定义（schema 直接给出，跳过派生）。"""

    return ToolDefinition(
        name=name,
        group=group,
        description=f"{name} 描述",
        permission="safe_read",
        handler=lambda: None,
        args_model=_PlaceholderArgs,
        parameters_schema={"type": "object", "properties": {}},
    )


@pytest.fixture
def tool_registry(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    """安装含两个分组的真实注册表，并让两处查询函数返回它。

    ``app.api.schemas`` 用同名类重导出了 ``AgentConfigurationDocument``，字符串路径会解析到类而
    不是模块，因此这里取模块对象打桩。
    """

    registry = ToolRegistry(
        [
            _definition("write_file", "文件"),
            _definition("read_file", "文件"),
            _definition("web_search", "联网"),
        ]
    )
    modules = [
        importlib.import_module("app.api.schemas.AgentConfigurationDocument"),
        importlib.import_module("app.api.schemas.response.AgentConfigurationResponse"),
    ]
    for module in modules:
        monkeypatch.setattr(module, "get_tool_registry", lambda: registry)
    return registry


def _payload(groups: list[str]) -> AgentConfigurationRequest:
    return AgentConfigurationRequest(
        agent_id="reviewer",
        role="child",
        description="审查代码",
        system_prompt="只审查，不修改",
        allowed_tool_groups=groups,
    )


def test_from_payload_expands_selected_groups_to_tool_names(tool_registry: ToolRegistry) -> None:
    document = AgentConfigurationDocument.from_payload(_payload(["文件"]))

    # 展开顺序跟随注册顺序，故按集合断言，不把排序口径钉在注册表实现细节上。
    assert set(document.allowed_tools) == {"read_file", "write_file"}


def test_from_payload_ignores_unknown_groups(tool_registry: ToolRegistry) -> None:
    """未知工具组当前不会被拒绝，只展开出空工具名（是否应报错见待确认项）。"""

    document = AgentConfigurationDocument.from_payload(_payload(["不存在"]))

    assert document.allowed_tools == []


def test_response_projects_tool_names_back_to_complete_groups(
    tool_registry: ToolRegistry,
) -> None:
    document = AgentConfigurationDocument(
        agent_id="reviewer",
        allowed_tools=["read_file", "write_file"],
    )

    response = AgentConfigurationResponse.from_document(document)

    assert set(response.allowed_tool_groups) == {"文件"}


def test_response_keeps_only_fully_selected_groups(tool_registry: ToolRegistry) -> None:
    """只选到分组内部分工具时，不把该分组回填给配置界面。"""

    document = AgentConfigurationDocument(agent_id="reviewer", allowed_tools=["read_file"])

    response = AgentConfigurationResponse.from_document(document)

    assert response.allowed_tool_groups == []
