"""等用户决定的调用（``ToolDefinition.user_decision``，目前是提问）在 Run 内如何结束：作答结算、
作废与并入前的判定。

决定的入队在 ``service/conversation/tool_decision_service.py``，等待占位由工具声明写下
（``tool_awaiting_decision``）；这里只放 workflow 侧的规则，workflow 节点与 actor 只调用。
"""
