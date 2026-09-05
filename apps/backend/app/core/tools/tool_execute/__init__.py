"""Tool execution primitives (executor pipeline, gate, runner, error factories).

Note:
    工具调用的跨调用编排（串行/并行分流、取消检查、异常收口）由
    ``app.core.workflows.workflow_operations.WorkflowOperations`` 承担；本包只提供
    单次调用的执行管线：``ToolExecutor``（门面）+ ``ToolAccessGate``（准入）+
    ``ToolHandlerRunner``（隔离执行）+ ``ToolObservationBudget``（双通道预算），
    以及 ``tool_error`` / ``tool_cancelled`` / ``tool_success`` 工厂与
    ``ToolTraceRecorder``。
"""
