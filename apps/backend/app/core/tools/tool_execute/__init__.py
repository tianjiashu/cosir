"""Tool execution primitives (scheduler, error factory, trace recorder).

Note:
    工具调用的跨调用编排（串行/并行分流、取消检查、异常收口）现由
    ``app.core.workflows.workflow_operations.WorkflowOperations`` 承担；本包只提供
    底层执行原语（``ToolScheduler``、``tool_error`` 工厂、``ToolTraceRecorder``）。
"""
