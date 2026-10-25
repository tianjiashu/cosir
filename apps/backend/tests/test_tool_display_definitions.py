"""Static Tool UI declarations for long-running tool shells."""

from app.core.tool.agent_team.agent_team_run import AgentTeamRunTool
from app.core.tool.child_agent.child_agent_create import DelegateTaskTool
from app.core.tool.child_agent.child_agent_send import ChildAgentSendTool
from app.core.tool.child_agent.child_agent_status import ChildAgentStatusTool
from app.core.tool.wait_messages import WaitMessagesTool


def test_child_agent_tools_declare_standalone_details_rows() -> None:
    """Child Agent 工具声明 standalone 展示面，且都不可展开。

    目的：固化 child_task/ 三个工具当前的 ``ToolDisplayHints`` 契约——三个工具都在
    独立区域强调结果（``standalone``），``expand_layout`` 统一为 ``details``，**都不**
    把模型结果正文暴露给客户端（``show_result=False``），也都不提供展开面
    （``delegate_task`` 的委派生命周期由独立卡片直接呈现，不再展开细节）。
    潜在缺陷：某个工具漏声明 ``display``，或展开面/结果暴露面与 UI 契约漂移，
    导致前端无法按 ``kind`` / ``expand_layout`` 正确路由。
    """

    delegate, send, status = (
        DelegateTaskTool.to_definition(object.__new__(DelegateTaskTool)),
        ChildAgentSendTool.to_definition(object.__new__(ChildAgentSendTool)),
        ChildAgentStatusTool.to_definition(object.__new__(ChildAgentStatusTool)),
    )

    for definition in (delegate, send, status):
        display = definition.display
        assert display is not None
        assert display.surface == "standalone"
        assert display.expand_layout == "details"
        assert display.show_result is False

    assert all(
        definition.display.expandable is False for definition in (delegate, send, status)
    )


def test_wait_messages_declares_a_plain_row_without_result() -> None:
    """挂起只是控制流：等待工具只显示动词与状态，不展开、不暴露结果正文、不产出展示 kind。"""

    display = WaitMessagesTool.to_definition(object.__new__(WaitMessagesTool)).display
    assert display is not None
    assert (display.surface, display.expand_layout) == ("standalone", "none")
    assert display.expandable is False and display.show_result is False


def test_agent_team_run_tool_advertises_only_running_existing_teams() -> None:
    """``agent_team`` 只运行已存在的 Team，描述与展示标题都不得暗示它能创建/配置 Team。

    目的：固化「运行」与「配置」的职责边界。历史上描述写作 "Prepare a configured Agent
    Team execution plan"、标题同样是英文 Prepare 口径，实测让模型在用户说「帮我配置个
    team」时反复选中本工具，而它只接受已存在的配置，于是以 ``agent_team_run_creation_invalid``
    失败收场。潜在缺陷：描述或标题重新出现「配置 / 准备 / Prepare / Configure」这类口径，
    再次把模型引向错误工具。
    """

    definition = AgentTeamRunTool.to_definition(object.__new__(AgentTeamRunTool))

    assert definition.description.startswith("Run an existing Agent Team")
    assert "cannot create or edit Team configurations" in definition.description

    display = definition.display
    assert display is not None
    for forbidden in ("Prepare", "Configur", "配置", "准备"):
        assert forbidden not in display.verb, f"agent_team 标题不得出现「{forbidden}」口径"
