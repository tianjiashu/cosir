"""单次工具调用的执行管线。

工具调用的跨调用编排（串行/并行分流、异常收口）由
``app.core.workflow.node_helper.tool_batch_runner.run_tool_batch`` 承担；本包只提供单次调用的
管线：``ToolExecutor``（门面）+ ``ToolAccessGate``（准入）+ ``ToolHandlerRunner``（隔离执行）+
``ToolObservationBudget``（模型通道预算与超限落盘）。结果构造工厂在 ``app.core.tool.results``。
"""
