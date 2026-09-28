"""Static Tool UI declarations for long-running tool shells."""

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
