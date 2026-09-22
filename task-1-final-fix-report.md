# Task 1 最后两个 P1 修复报告

## 修复内容

- `WorkflowOperations.run_tool_calls` 对不在模型工具映射中的工具名显式走同步 `ToolExecutor` 入口，由准入门禁生成稳定的 `unknown tool` error；`_is_parallel_call` 对未知或映射不完整的工具显式返回 `False`。
- `ToolHandlerRunner` 暴露 `build_cancellation_check` 与 `cleanup_cancellation_signal` public contract。`ToolExecutor.execute_async` 通过非阻塞 asyncio 轮询复用同一取消 registry 语义：工具级取消后取消并等待 async handler task 收口，先投影 `cancelled` 再重新抛出 `CancelledError`，finally 清理工具调用取消信号。
- 新增专项回归覆盖未知工具路由，以及 `tool_call_cancellation_registry.mark_cancelled → execute_async` 的有界取消、终态投影顺序和信号清理。

## 验证结果

通过：

- `tests/test_task1_async_tools_and_child_wait.py`：16 passed
- `tests/test_parallel_tool_calls_nonblocking.py tests/test_tool_call_cancellation.py`：26 passed
- 定向 ruff（本次修改的 4 个文件）：All checks passed

相邻验证中的既有环境/基线问题：

- `tests/test_tool_handler_runner_cancellation.py`：28 passed，2 failed；两个 fake process 用例在线程替身中执行 `os.setsid()`，当前沙箱返回 `PermissionError: [Errno 1] Operation not permitted`，与本次修改无关。
- Task 1 组合测试：58 passed，2 failed；`test_workflow_operations_tool_message.py` 的两个旧断言仍要求输出 `retryable: true/false` 行，而当前工作树实现只输出受控 hint/reason，本次未改该逻辑。
- 后端全量 `ruff check .`：工作树已有 118 个 lint 问题，未落在本次修改文件；本次修改文件的定向检查通过。

未修改 frontend/Task 2，未提交 Git。
