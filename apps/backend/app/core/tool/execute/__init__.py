"""单次工具调用的执行管线。

工具调用的跨调用编排（顺序执行、整批拒绝、异常收口）由
``app.core.workflow.nodes.tools_node`` 承担；本包只提供单次调用的
管线：``ToolExecutor``（编排并调用 handler）+ ``ToolAccessGate``（准入）+
``ToolOutputBudget``（模型通道预算与超限落盘）。结果构造工厂在 ``app.core.tool.results``。
"""
