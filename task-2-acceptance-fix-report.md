# Task 2 Final Acceptance Fix Report

日期：2026-09-22

## 范围

本次只修改 backend 代码和回归测试；未修改 frontend，未提交 git。没有删除
`ChildAgentRunner.run_child` / `asyncio.run` compatibility scaffold，因为当前仓库仍保留
相关测试和兼容入口，无法证明删除不会改变兼容行为。

## 修复内容

### 1. ConversationRunExecutor 与 finalization canonical I/O

- `ConversationRunExecutor.start()` 的 Run 状态读取改为 `asyncio.to_thread`。
- `start_registered()` 现在只负责事件循环归属的进程内 task 注册，不含 SQLite 读取；child
  session 在持有短锁时仍能保持无 await 的注册临界区。
- `_execute()` 的初始 `get_run`、失败路径的 tool-settlement projector 读取改为 worker thread。
- `cancel()` 和 `cancel_tool_call()` 保留第一次 await 前的进程内取消信号标记，再在线程池中做
  canonical 存在性检查；API 的返回值和 `KeyError` 语义不变。
- `ConversationRunStateService` 的 completed/failed/cancelled finalization 直接使用条件更新返回
  的 canonical `record` 通知 observer，移除终态提交后的同步 `.get()` 回读。

### 2. lifespan cleanup

- startup-failure cleanup 中的 wait coordinator shutdown、terminal shutdown、
  `close_service_dependencies` 和外层 logging shutdown 均移出 event loop。
- 正常 shutdown 中的 Langfuse flush、`close_service_dependencies`、terminal/logging shutdown
  均通过 `asyncio.to_thread` 执行。
- 原有资源关闭顺序保留；startup cleanup 仍逐段 best-effort 记录错误，不覆盖原始 startup 异常。

### 3. Child Agent finalization Future

- `ChildAgentSessionService._finalization_workers.submit()` 返回的 Future 现在挂有 done callback，
  callback 消费 `future.result()` 并记录 `run_id`、`task_id`、异常类型和消息。
- child finalization 的 wait 通知失败不会跳过 follow-up 调度。
- follow-up 调度异常会记录结构化日志、清除 inflight 标记并保留 `follow_up_pending`，供有界
  sweep 重试。

## 回归测试

新增：`apps/backend/tests/test_task2_acceptance_async_boundaries.py`，覆盖：

- executor start/execute/cancel/cancel-tool-call/projector canonical reads 不运行在 event-loop
  thread；
- state finalization 不再同步 canonical 回读；
- startup-failure 与正常 shutdown dependency cleanup 不阻塞 event loop；
- finalization worker Future 异常日志；
- child wait 通知失败后 follow-up 仍可调度。

验证结果：

- Task1、Task2、race/lifecycle/executor 矩阵：**74 passed**
- 新增 acceptance 回归单独运行：**10 passed**
- backend `compileall`：通过
- 目标修改文件 Ruff：通过

命令：

```text
UV_CACHE_DIR=/tmp/coding-agent-uv-cache uv run pytest \
  tests/test_task1_async_tools_and_child_wait.py \
  tests/test_task2_child_agent_sessions.py \
  tests/test_task2_lifecycle_cleanup.py \
  tests/test_task2_acceptance_async_boundaries.py \
  tests/test_conversation_run_executor_cancel.py \
  tests/test_run_zombie_convergence.py \
  tests/test_parallel_tool_calls_nonblocking.py -q

UV_CACHE_DIR=/tmp/coding-agent-uv-cache uv run python -m compileall -q app tests

UV_CACHE_DIR=/tmp/coding-agent-uv-cache uv run ruff check \
  app/assistant_transport/service/conversation_run_executor.py \
  app/service/task/conversation_run_state_service.py \
  app/lifespan.py \
  app/service/child_agent/child_agent_session_service.py \
  tests/test_task2_acceptance_async_boundaries.py \
  tests/test_task2_lifecycle_cleanup.py
```

## 已知基线问题

全仓 `ruff check .` 当前报告 118 个问题，分布在本次目标文件之外的既有代码和测试中；本次
没有为消除这些无关问题扩大修改范围。

`tests/test_lifespan_split_adversarial.py` 当前结果为 **56 passed, 8 errors**。8 个 fixture
setup error 都是既有测试导入 `app.hook.hook_registry`，而当前代码目录为
`app.core.hook` 的 `ModuleNotFoundError`；本次没有引入兼容 alias，也没有修改该非目标基线。
