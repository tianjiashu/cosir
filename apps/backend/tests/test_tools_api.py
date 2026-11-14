from types import SimpleNamespace
from typing import ClassVar

import pytest

import app.api.tools_api as tools_api


@pytest.mark.asyncio
async def test_tool_groups_returns_sorted_group_names_of_main_agent_tools(monkeypatch) -> None:
    class Profile:
        allowed_tools: ClassVar[tuple[str, ...]] = ("read_file", "web_search")

        def select_tools(self, definitions):
            return [tool for tool in definitions if tool.name in self.allowed_tools]

    definitions = [
        SimpleNamespace(name="write_file", group="编辑", description="write"),
        SimpleNamespace(name="web_search", group="联网", description="search"),
        SimpleNamespace(name="read_file", group="文件", description="read"),
    ]
    monkeypatch.setattr(
        tools_api,
        "get_agent_registry",
        lambda: SimpleNamespace(resolve=lambda _workspace, _agent_id: Profile()),
    )
    monkeypatch.setattr(
        tools_api,
        "get_tool_registry",
        lambda: SimpleNamespace(get_all_definitions=lambda: definitions),
    )

    response = await tools_api.get_tool_groups()

    assert response == {"groups": ["文件", "联网"]}
