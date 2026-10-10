"""Static Tool UI declarations for long-running tool shells."""

from app.core.tools.tool_handler.agent_team.agent_team_run import AgentTeamRunTool
from app.core.tools.tool_handler.child_task.child_agent_create import DelegateTaskTool
from app.core.tools.tool_handler.child_task.child_agent_send import ChildAgentSendTool
from app.core.tools.tool_handler.child_task.child_agent_status import ChildAgentStatusTool
from app.core.tools.tool_handler.child_task.child_agent_wait import ChildAgentWaitTool


def test_child_agent_tools_declare_standalone_details_rows() -> None:
    """Child Agent 工具声明 standalone 展示面；仅 delegate_task 允许展开安全细节。

    目的：固化 child_task/ 四个工具当前的 ``ToolDisplayHints`` 契约——四个工具都在
    独立区域强调结果（``standalone``），``expand_layout`` 统一为 ``details``，且**都不**
    把模型结果正文暴露给客户端（``show_result=False``）；其中只有发起委派的
    ``delegate_task`` 可展开（携带 child locator 的委派生命周期细节）。
    潜在缺陷：某个工具漏声明 ``display``，或展开面/结果暴露面与 UI 契约漂移，
    导致前端无法按 ``kind`` / ``expand_layout`` 正确路由。
    """

    delegate, send, status, wait = (
        DelegateTaskTool.to_definition(object.__new__(DelegateTaskTool)),
        ChildAgentSendTool.to_definition(object.__new__(ChildAgentSendTool)),
        ChildAgentStatusTool.to_definition(object.__new__(ChildAgentStatusTool)),
        ChildAgentWaitTool.to_definition(object.__new__(ChildAgentWaitTool)),
    )

    for definition in (delegate, send, status, wait):
        display = definition.display
        assert display is not None
        assert display.surface == "standalone"
        assert display.expand_layout == "details"
        assert display.show_result is False

    assert delegate.display.expandable is True
    assert all(definition.display.expandable is False for definition in (send, status, wait))


def test_agent_team_run_tool_advertises_only_running_existing_teams() -> None:
    """``agent_team`` 只运行已存在的 Team，描述与展示标题都不得暗示它能创建/配置 Team。

    目的：固化「运行」与「配置」的职责边界。历史上描述写作 "Prepare a configured Agent
    Team execution plan"、标题同样是英文 Prepare 口径，实测让模型在用户说「帮我配置个
    team」时反复选中本工具，而它只接受已存在的配置，于是以 ``agent_team_run_creation_invalid``
    失败收场。潜在缺陷：描述或标题重新出现「配置 / 准备 / Prepare / Configure」这类口径，
    再次把模型引向错误工具。
    """

    definition = AgentTeamRunTool.to_definition(object.__new__(AgentTeamRunTool))

    assert "must already exist" in definition.description
    assert "cannot create or modify" in definition.description

    display = definition.display
    assert display is not None
    for forbidden in ("Prepare", "Configur", "配置", "准备"):
        assert forbidden not in display.verb, f"agent_team 标题不得出现「{forbidden}」口径"
