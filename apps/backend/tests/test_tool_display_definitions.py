"""工具静态展示声明：只剩 verb / icon，口径不得暗示错误能力。"""

from app.core.tool.handler.agent_team_run import AgentTeamRunTool
from app.core.tool.handler.wait_messages import WaitMessagesTool
from app.core.tool.schemas import ToolDisplayHints


def test_display_hints_only_declare_verb_and_icon() -> None:
    """分区、能否展开、布局都由前端按 kind 与 awaitingUser 决定，后端声明只剩两个字段。"""

    assert ToolDisplayHints(verb="读取文件", icon="eye").to_dict() == {
        "verb": "读取文件",
        "icon": "eye",
    }


def test_wait_messages_heading_has_no_target() -> None:
    """挂起只是控制流：标题只有动词，没有目标，也不产出展示 kind。"""

    definition = WaitMessagesTool.to_definition(object.__new__(WaitMessagesTool))
    assert definition.display is not None and definition.heading is not None
    heading = definition.heading(definition.args_model())
    assert (heading.title, heading.target) == (definition.display.verb, None)


def test_agent_team_run_tool_advertises_only_running_existing_teams() -> None:
    """``agent_team`` 只运行已存在的 Team，描述与展示标题都不得暗示它能创建/配置 Team。

    历史上描述写作 "Prepare a configured Agent Team execution plan"、标题同样是英文 Prepare
    口径，实测让模型在用户说「帮我配置个 team」时反复选中本工具，而它只接受已存在的配置，于是以
    ``agent_team_run_creation_invalid`` 失败收场。
    """

    definition = AgentTeamRunTool.to_definition(object.__new__(AgentTeamRunTool))

    assert definition.description.startswith("Run an existing Agent Team")
    assert "cannot create or edit Team configurations" in definition.description

    display = definition.display
    assert display is not None
    for forbidden in ("Prepare", "Configur", "配置", "准备"):
        assert forbidden not in display.verb, f"agent_team 标题不得出现「{forbidden}」口径"
